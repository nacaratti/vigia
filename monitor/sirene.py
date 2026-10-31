"""Sirene pelas caixas de som do PC: tom de alarme + aviso de voz, em loop.

O áudio é gerado localmente na primeira execução (data/sirene.wav):
- sirene sintetizada (tom subindo e descendo, bem audível);
- aviso de voz pela síntese do Windows (voz pt-BR, se instalada).
O volume é o do Windows: deixe as caixas de som ligadas e com volume alto.
"""
import logging
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .config import RAIZ

log = logging.getLogger("sirene")
TAXA = 44100
PASTA = RAIZ / "data"
ARQ_SIRENE = PASTA / "sirene.wav"
ARQ_VOZ = PASTA / "sirene_voz.wav"
MENSAGEM = "Atenção! Esta área é monitorada. Você está sendo filmado."


def _tom_sirene(segundos=4.0) -> np.ndarray:
    """Sirene 'wail': frequência sobe e desce entre 650 e 1500 Hz a cada segundo."""
    t = np.arange(int(TAXA * segundos)) / TAXA
    freq = 650 + 850 * (0.5 - 0.5 * np.cos(2 * np.pi * t / 1.0))
    fase = 2 * np.pi * np.cumsum(freq) / TAXA
    onda = np.tanh(3 * np.sin(fase))  # levemente saturada: mais "presença" em caixas pequenas
    return (onda / np.max(np.abs(onda)) * 0.95).astype(np.float32)


def _ler_wav(caminho: Path) -> np.ndarray | None:
    try:
        with wave.open(str(caminho)) as w:
            if w.getframerate() != TAXA or w.getsampwidth() != 2:
                return None
            dados = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
            if w.getnchannels() == 2:
                dados = dados.reshape(-1, 2).mean(axis=1)
            pico = np.max(np.abs(dados)) or 1
            return dados / pico * 0.95
    except (OSError, wave.Error):
        return None


def _gerar_voz(texto: str) -> bool:
    """Gera ARQ_VOZ com a síntese de voz do Windows (prefere voz pt-BR)."""
    if sys.platform != "win32":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'pt-BR' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
        "$s.Rate = -1;"
        f"$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({TAXA}, 'Sixteen', 'Mono');"
        f"$s.SetOutputToWaveFile('{ARQ_VOZ}', $fmt);"
        f"$s.Speak('{texto}'); $s.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ARQ_VOZ.exists()


def preparar_audio(com_voz=True) -> Path:
    """Monta data/sirene.wav = sirene + voz + sirene (uma volta do loop)."""
    PASTA.mkdir(exist_ok=True)
    partes = [_tom_sirene(4.0)]
    if com_voz and (ARQ_VOZ.exists() or _gerar_voz(MENSAGEM)):
        voz = _ler_wav(ARQ_VOZ)
        if voz is not None:
            silencio = np.zeros(int(TAXA * 0.3), np.float32)
            partes += [silencio, voz, silencio, _tom_sirene(4.0)]
    audio = np.concatenate(partes)
    with wave.open(str(ARQ_SIRENE), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    return ARQ_SIRENE


class Sirene:
    """Toca data/sirene.wav em loop por até `segundos`. Thread-safe."""

    def __init__(self, ao_mudar=None):
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.tocando = False
        self.desde = 0.0
        self.motivo = ""
        self.ao_mudar = ao_mudar  # callback(tocando: bool, motivo: str, quem: str)
        self.disponivel = sys.platform == "win32"
        if self.disponivel and not ARQ_SIRENE.exists():
            try:
                preparar_audio()
            except Exception:
                log.exception("Não foi possível gerar o áudio da sirene")
                self.disponivel = False

    def disparar(self, segundos: int, motivo: str) -> bool:
        if not self.disponivel:
            log.warning("Sirene indisponível neste sistema")
            return False
        import winsound

        with self._lock:
            ja = self.tocando
            winsound.PlaySound(str(ARQ_SIRENE), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(segundos, self.parar, kwargs={"quem": "tempo esgotado"})
            self._timer.daemon = True
            self._timer.start()
            self.tocando, self.motivo = True, motivo
            if not ja:
                self.desde = time.time()
        if not ja:
            log.warning("SIRENE LIGADA (%s) por até %ss", motivo, segundos)
            if self.ao_mudar:
                self.ao_mudar(True, motivo, "")
        return True

    def parar(self, quem: str = "painel"):
        if not self.disponivel:
            return
        import winsound

        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            estava = self.tocando
            winsound.PlaySound(None, 0)  # None interrompe o som em reprodução
            self.tocando = False
        if estava:
            log.info("Sirene desligada (%s)", quem)
            if self.ao_mudar:
                self.ao_mudar(False, self.motivo, quem)

    def status(self) -> dict:
        return {"disponivel": self.disponivel, "tocando": self.tocando,
                "ha_s": round(time.time() - self.desde) if self.tocando else 0, "motivo": self.motivo}
