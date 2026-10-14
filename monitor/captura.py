"""Captura contínua por câmera: mantém só o frame mais recente, reconecta com backoff."""
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402

try:  # só erros do OpenCV/FFmpeg (a API mudou entre versões)
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

BACKOFF_MAX = 60
PAUSA_SENHA_S = 1800  # após 401, todas as câmeras RTSP esperam 30 min

# DVRs Intelbras/Dahua bloqueiam o usuário após ~5 logins errados. Uma recusa
# de senha pausa TODAS as fontes RTSP de uma vez, em vez de cada uma insistir.
_pausa_senha = {"ate": 0.0}
_lock_senha = threading.Lock()
log_cap = logging.getLogger("captura")


def senha_recusada(url: str) -> bool:
    """Usa ffprobe para saber se a falha foi 401 (o OpenCV não informa o motivo)."""
    if not shutil.which("ffprobe"):
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000", "-i", url],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "401" in r.stderr or "Unauthorized" in r.stderr


def verificar_credenciais(url: str, senha: str) -> str:
    """'' se pode conectar; senão o motivo. Faz no máximo UMA tentativa de login."""
    if not senha or senha == "troque-me":
        return "senha do DVR não configurada no .env"
    if senha_recusada(url):
        return "usuário/senha recusados pelo DVR; corrija o .env e reinicie"
    return ""


class Fonte(threading.Thread):
    """Lê o stream sem parar e guarda apenas o último frame.

    Ler continuamente (em vez de só quando vamos analisar) evita que o buffer
    do FFmpeg acumule frames atrasados.
    """

    def __init__(self, cam_id: str, url: str, bloqueio: str = ""):
        super().__init__(name=f"captura-{cam_id}", daemon=True)
        self.bloqueio = bloqueio  # motivo para nem tentar conectar
        self.cam_id = cam_id
        self.url = url
        self.eh_arquivo = Path(url).is_file()
        self.log = logging.getLogger(f"captura.{cam_id}")
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        self._ts = 0.0
        self.conectado = False
        self.fps = 0.0
        self.reconexoes = 0
        self.ultimo_erro = ""
        self.resolucao = None

    def ultimo(self):
        """(frame, seq, timestamp). O frame não deve ser modificado por quem lê."""
        with self._lock:
            return self._frame, self._seq, self._ts

    def parar(self):
        self._parar.set()

    def _abrir(self):
        if self.eh_arquivo:
            return cv2.VideoCapture(self.url)
        return cv2.VideoCapture(
            self.url, cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000],
        )

    def run(self):
        if self.bloqueio:
            self.ultimo_erro = self.bloqueio
            return
        espera = 1
        while not self._parar.is_set():
            if not self.eh_arquivo and time.time() < _pausa_senha["ate"]:
                self.ultimo_erro = "senha recusada pelo DVR; aguardando antes de tentar de novo"
                self._parar.wait(10)
                continue
            cap = self._abrir()
            if not cap.isOpened():
                cap.release()
                self.conectado = False
                self.ultimo_erro = "não foi possível abrir o stream"
                if not self.eh_arquivo:
                    with _lock_senha:
                        if time.time() >= _pausa_senha["ate"] and senha_recusada(self.url):
                            _pausa_senha["ate"] = time.time() + PAUSA_SENHA_S
                            log_cap.error("DVR recusou a senha. Todas as câmeras pausadas por %s min "
                                          "para evitar bloqueio do usuário.", PAUSA_SENHA_S // 60)
                    if time.time() < _pausa_senha["ate"]:
                        continue
                self.log.warning("Falha ao conectar; nova tentativa em %ss", espera)
                self._parar.wait(espera)
                espera = min(espera * 2, BACKOFF_MAX)
                self.reconexoes += 1
                continue

            self.log.info("Conectado")
            self.conectado, self.ultimo_erro = True, ""
            fps_arquivo = (cap.get(cv2.CAP_PROP_FPS) or 15) if self.eh_arquivo else 0
            janela_ini, janela_n = time.time(), 0
            while not self._parar.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    if self.eh_arquivo:  # modo teste: volta ao início do vídeo
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.ultimo_erro = "stream interrompido"
                    self.log.warning("Stream interrompido; reconectando")
                    break
                agora = time.time()
                janela_n += 1
                if agora - janela_ini >= 3:  # média em janelas de 3 s (ignora a rajada inicial do buffer)
                    self.fps = janela_n / (agora - janela_ini)
                    janela_ini, janela_n = agora, 0
                with self._lock:
                    self._frame, self._ts = frame, agora
                    self._seq += 1
                self.resolucao = (frame.shape[1], frame.shape[0])
                espera = 1  # recebeu frame: zera o backoff
                if fps_arquivo:  # arquivo: respeita o tempo real do vídeo
                    time.sleep(max(0, 1 / fps_arquivo - (time.time() - t0)))
            cap.release()
            self.conectado = False
            if not self._parar.is_set():
                self.reconexoes += 1
                self._parar.wait(espera)
                espera = min(espera * 2, BACKOFF_MAX)
