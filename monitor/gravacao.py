"""Gravação de vídeo dos eventos, com alguns segundos de pré-gravação."""
import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

PRE_FPS = 5  # a pré-gravação guarda 5 quadros/s (em JPEG) para economizar memória


class Gravador(threading.Thread):
    """Um por câmera. Enquanto a câmera está vigiando, mantém os últimos `pre_s`
    segundos em memória; quando um evento começa, grava pré-gravação + ao vivo
    até `pos_s` segundos depois da última pessoa vista (máximo `max_s`)."""

    def __init__(self, monitor):
        super().__init__(name=f"gravacao-{monitor.cam['id']}", daemon=True)
        self.monitor = monitor
        self.fonte = monitor.fonte
        self.log = logging.getLogger(f"gravacao.{monitor.cam['id']}")
        self._parar = threading.Event()
        self._pedido = None  # (evento_id, caminho)
        self.buffer: deque = deque()  # (ts, jpeg)
        self.gravando = False
        self.ffmpeg = shutil.which("ffmpeg")

    def parar(self):
        self._parar.set()

    def iniciar(self, evento_id: int, caminho: Path) -> bool:
        if self.gravando or self._pedido:
            return False
        self._pedido = (evento_id, caminho)
        return True

    @property
    def cfg(self) -> dict:
        return self.monitor.sistema.cfg["gravacao"]

    def run(self):
        ultimo_seq = -1
        while not self._parar.is_set():
            if self._pedido:
                eid, caminho = self._pedido
                try:
                    self.gravando = True
                    if self._gravar(caminho) and not self.monitor.sistema.eventos.definir_video(eid, caminho):
                        caminho.unlink(missing_ok=True)  # o evento foi apagado durante a gravação
                except Exception:
                    self.log.exception("Erro na gravação")
                finally:
                    self.gravando = False
                    self._pedido = None
                continue
            cfg = self.cfg
            if not cfg["ativo"] or not self.monitor.em_janela or cfg["pre_s"] <= 0:
                self.buffer.clear()
                self._parar.wait(0.5)
                continue
            frame, seq, ts = self.fonte.ultimo()
            if frame is not None and seq != ultimo_seq:
                ultimo_seq = seq
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    self.buffer.append((ts, jpg))
                while self.buffer and ts - self.buffer[0][0] > cfg["pre_s"]:
                    self.buffer.popleft()
            self._parar.wait(1 / PRE_FPS)

    def _abrir_saida(self, caminho: Path, w: int, h: int, fps: int):
        if self.ffmpeg:  # H.264: toca no navegador e no celular
            proc = subprocess.Popen(
                [self.ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(caminho)],
                stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            return lambda f: proc.stdin.write(f.tobytes()), lambda: (proc.stdin.close(), proc.wait(60))
        out = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        return out.write, out.release

    def _gravar(self, destino: Path) -> bool:
        cfg = self.cfg
        frame, _, _ = self.fonte.ultimo()
        if frame is None:
            return False
        h, w = frame.shape[:2]
        fps = int(min(30, max(5, round(self.fonte.fps or 15))))
        tmp = destino.with_suffix(".parcial.mp4")
        escrever, fechar = self._abrir_saida(tmp, w, h, fps)
        inicio = time.time()
        quadros = 0

        def gravar(f):
            nonlocal quadros
            if f.shape[:2] != (h, w):
                f = cv2.resize(f, (w, h))
            escrever(f)
            quadros += 1

        try:
            # Pré-gravação: cada quadro guardado é repetido para manter o tempo real.
            pre = list(self.buffer)
            self.buffer.clear()
            for i, (ts, jpg) in enumerate(pre):
                prox = pre[i + 1][0] if i + 1 < len(pre) else inicio
                f = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                for _ in range(max(1, round((prox - ts) * fps))):
                    gravar(f)

            # Ao vivo, no ritmo da câmera, até a pessoa sumir há `pos_s` segundos.
            proximo = time.time()
            while not self._parar.is_set():
                agora = time.time()
                if agora - self.monitor._ultima_presenca > cfg["pos_s"] or agora - inicio > cfg["max_s"]:
                    break
                frame, _, _ = self.fonte.ultimo()
                if frame is not None:
                    gravar(frame)
                proximo += 1 / fps
                self._parar.wait(max(0, proximo - time.time()))
        finally:
            fechar()
        if quadros == 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(destino)
        self.log.info("Vídeo salvo: %s (%.0fs)", destino.name, quadros / fps)
        return True
