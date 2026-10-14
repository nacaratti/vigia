"""Gera um vídeo de teste (sem precisar de ninguém na frente da câmera).

Alterna cenas vazias com as fotos de exemplo do Ultralytics (que têm pessoas),
com leve movimento, para exercitar K-de-N, cooldown e registro de eventos.

    python scripts/gerar_video_teste.py            # cria data/video_teste.mp4
    python main.py --video data/video_teste.mp4 --sem-alerta
"""
from pathlib import Path

import cv2
import numpy as np
from ultralytics.utils import ASSETS

RAIZ = Path(__file__).resolve().parent.parent
W, H, FPS = 960, 540, 15


def ajustar(img):
    h, w = img.shape[:2]
    escala = min(W / w, H / h)
    img = cv2.resize(img, (int(w * escala), int(h * escala)))
    tela = np.full((H, W, 3), 30, np.uint8)
    y, x = (H - img.shape[0]) // 2, (W - img.shape[1]) // 2
    tela[y:y + img.shape[0], x:x + img.shape[1]] = img
    return tela


def cena_vazia(segundos, rotulo):
    base = np.full((H, W, 3), 40, np.uint8)
    cv2.rectangle(base, (0, H * 2 // 3), (W, H), (55, 60, 55), -1)  # "chão"
    for i in range(int(segundos * FPS)):
        f = base.copy()
        ruido = np.random.randint(0, 6, f.shape, np.uint8)
        cv2.putText(f, rotulo, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
        yield cv2.add(f, ruido)


def cena_pessoa(img, segundos, rotulo):
    base = ajustar(img)
    for i in range(int(segundos * FPS)):
        dx = int(20 * np.sin(i / 10))  # balanço leve, como uma câmera real
        f = cv2.warpAffine(base, np.float32([[1, 0, dx], [0, 1, 0]]), (W, H), borderValue=(30, 30, 30))
        cv2.putText(f, rotulo, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        yield f


def main():
    destino = RAIZ / "data" / "video_teste.mp4"
    destino.parent.mkdir(exist_ok=True)
    bus, zidane = cv2.imread(str(ASSETS / "bus.jpg")), cv2.imread(str(ASSETS / "zidane.jpg"))
    out = cv2.VideoWriter(str(destino), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    cenas = [
        cena_vazia(8, "vazio"),
        cena_pessoa(zidane, 8, "2 pessoas"),
        cena_vazia(6, "vazio"),
        cena_pessoa(bus, 8, "pessoas + onibus"),
        cena_vazia(4, "vazio"),
    ]
    n = 0
    for cena in cenas:
        for f in cena:
            out.write(f)
            n += 1
    out.release()
    print(f"Vídeo criado: {destino.relative_to(RAIZ)} ({n / FPS:.0f}s, {W}x{H})")


if __name__ == "__main__":
    main()
