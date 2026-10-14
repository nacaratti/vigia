"""Detecção de pessoas (YOLO), filtro por zonas e desenho das anotações."""
import logging
import os
import threading
from pathlib import Path

import cv2
import numpy as np

from .config import RAIZ

PASTA_MODELOS = RAIZ / "models"
log = logging.getLogger("deteccao")

COR_PESSOA = (40, 60, 240)      # BGR: vermelho, pessoa dentro da zona
COR_IGNORADA = (160, 160, 160)  # cinza, pessoa fora das zonas
COR_ZONA = (60, 200, 255)       # âmbar
COR_PET = (255, 170, 60)        # azul, pet reconhecido (nunca alerta)

PESSOA = 0
PETS = {15: "gato", 16: "cachorro"}  # classes COCO
CONF_PET = 0.25        # pets contam a partir desta confiança (só servem para descartar enganos)
IOU_PET = 0.5          # sobreposição a partir da qual "pessoa" e "pet" são o mesmo objeto
MARGEM_PET = 0.15      # "pessoa" é descartada se o pet tiver confiança >= a dela - margem

# Segunda opinião: um classificador ImageNet olha o recorte de cada "pessoa".
# As classes 0-397 do ImageNet são todas animais (cães, gatos, e também os palpites
# que o modelo dá para um cachorro em pose estranha: leão-marinho, macaco...).
# Pessoas vestidas caem em roupas/objetos. Medido nos vídeos reais da casa:
# P(animal) de pessoas 0,00-0,07 (mediana), de cachorros 0,70-0,96.
MODELO_ANIMAL = "yolo11s-cls.pt"
CLASSES_ANIMAIS = 398


def _iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    uniao = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / uniao if uniao > 0 else 0.0


class Detector:
    """Um único modelo compartilhado por todas as câmeras (inferência serializada)."""

    def __init__(self, modelo: str):
        import torch
        from ultralytics import YOLO

        # Por padrão o PyTorch usa todos os núcleos e fica em espera ativa entre
        # inferências, ocupando a CPU à toa. Poucas threads rendem quase o mesmo.
        torch.set_num_threads(int(os.getenv("DETECCAO_THREADS", "4")))
        PASTA_MODELOS.mkdir(exist_ok=True)
        self.nome = modelo
        caminho = PASTA_MODELOS / modelo
        log.info("Carregando modelo %s", modelo)
        self.modelo = YOLO(str(caminho))
        self.classificador = None  # carregado na primeira suspeita
        self._lock = threading.Lock()

    def prob_animal(self, frame, p) -> float:
        """Probabilidade (0-1) de o recorte da caixa `p` ser um animal. Chamar com o lock."""
        if self.classificador is None:
            from ultralytics import YOLO
            log.info("Carregando classificador de animais %s", MODELO_ANIMAL)
            self.classificador = YOLO(str(PASTA_MODELOS / MODELO_ANIMAL))
        x1, y1, x2, y2 = p[:4]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        lado = max(max(x2 - x1, y2 - y1) * 1.6, 96)  # mesma margem usada na avaliação
        h, w = frame.shape[:2]
        recorte = frame[int(max(0, cy - lado / 2)):int(min(h, cy + lado / 2)),
                        int(max(0, cx - lado / 2)):int(min(w, cx + lado / 2))]
        if recorte.size == 0:
            return 0.0
        probs = self.classificador.predict(recorte, imgsz=224, verbose=False)[0].probs.data
        return float(probs[:CLASSES_ANIMAIS].sum())

    def detectar(self, frame, confianca: float, imgsz: int, limite_animal: float | None = None) -> tuple[list, list, int]:
        """(pessoas, pets, descartadas).

        pessoas: [(x1, y1, x2, y2, conf)]; pets: [(x1, y1, x2, y2, conf, nome)].
        Uma "pessoa" que é na verdade o mesmo objeto que um cachorro/gato
        (caixas sobrepostas, confiança parecida) é descartada; à noite, no
        infravermelho, um cachorro em pé às vezes parece gente.
        """
        with self._lock:
            r = self.modelo.predict(frame, classes=[PESSOA, *PETS], conf=min(confianca, CONF_PET),
                                    imgsz=imgsz, verbose=False)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return [], [], 0
        pessoas, pets = [], []
        for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
            caixa = tuple(int(v) for v in b)
            if int(k) == PESSOA:
                if c >= confianca:
                    pessoas.append((*caixa, float(c)))
            elif c >= CONF_PET:
                pets.append((*caixa, float(c), PETS[int(k)]))
        validas = [p for p in pessoas
                   if not any(_iou(p, a) >= IOU_PET and a[4] >= p[4] - MARGEM_PET for a in pets)]
        if limite_animal and validas:
            confirmadas = []
            with self._lock:
                for p in validas:
                    pa = self.prob_animal(frame, p)
                    if pa >= limite_animal:  # cachorro/gato que o detector chamou de pessoa
                        pets.append((*p[:4], pa, "animal"))
                    else:
                        confirmadas.append(p)
            validas = confirmadas
        return validas, pets, len(pessoas) - len(validas)


def _poligono_px(zona, w, h):
    return np.array([[x * w, y * h] for x, y in zona], dtype=np.float32)


def separar_por_zona(deteccoes, zonas, w, h):
    """(dentro, fora). Sem zonas, tudo conta. Usa os pés (base central da caixa)."""
    if not zonas:
        return list(deteccoes), []
    polis = [_poligono_px(z, w, h) for z in zonas]
    dentro, fora = [], []
    for d in deteccoes:
        pe = ((d[0] + d[2]) / 2, float(d[3]))
        alvo = dentro if any(cv2.pointPolygonTest(p, pe, False) >= 0 for p in polis) else fora
        alvo.append(d)
    return dentro, fora


def desenhar(frame, dentro, fora, zonas, pets=()):
    """Desenha zonas e caixas sobre uma CÓPIA do frame."""
    img = frame.copy()
    h, w = img.shape[:2]
    esp = max(1, round(w / 400))
    if zonas:
        camada = img.copy()
        for z in zonas:
            p = _poligono_px(z, w, h).astype(np.int32)
            cv2.fillPoly(camada, [p], COR_ZONA)
            cv2.polylines(img, [p], True, COR_ZONA, esp)
        cv2.addWeighted(camada, 0.15, img, 0.85, 0, img)
    itens = [(COR_PET, p[:5], p[5]) for p in pets]
    itens += [(COR_IGNORADA, p, "pessoa") for p in fora] + [(COR_PESSOA, p, "pessoa") for p in dentro]
    for cor, (x1, y1, x2, y2, c), nome in itens:
        cv2.rectangle(img, (x1, y1), (x2, y2), cor, esp + 1)
        rotulo = f"{nome} {c:.0%}"
        escala = 0.4 * esp
        (tw, th), _ = cv2.getTextSize(rotulo, cv2.FONT_HERSHEY_SIMPLEX, escala, esp)
        y0 = max(y1, th + 6)
        cv2.rectangle(img, (x1, y0 - th - 6), (x1 + tw + 6, y0), cor, -1)
        cv2.putText(img, rotulo, (x1 + 3, y0 - 4), cv2.FONT_HERSHEY_SIMPLEX, escala, (255, 255, 255), esp, cv2.LINE_AA)
    return img


def modelo_disponivel(modelo: str) -> bool:
    return (PASTA_MODELOS / modelo).exists() or Path(modelo).exists()
