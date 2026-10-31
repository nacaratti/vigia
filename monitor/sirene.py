"""Sirene pelas caixas de som do PC: som de alarme + aviso de voz, em loop.

Sons em assets/sons/ (sirenes reais em domínio público e o aviso em voz neural; ver
assets/sons/CREDITOS.md). O usuário escolhe o som no painel ou envia o próprio arquivo
(data/sirene_personalizada.wav). A volta do loop (sirene + voz + sirene) é montada em
data/sirene_<som>[_voz].wav sempre que a escolha muda.
O volume é o do Windows: deixe as caixas de som ligadas e com volume alto.
"""
import io
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
SONS = RAIZ / "assets" / "sons"
ARQ_VOZ_NEURAL = SONS / "voz_aviso.wav"
ARQ_VOZ_WINDOWS = PASTA / "sirene_voz.wav"  # reserva, se o arquivo da voz neural faltar
ARQ_PERSONALIZADO = PASTA / "sirene_personalizada.wav"
MENSAGEM = "Atenção! Esta área é monitorada. Você está sendo filmado, e o proprietário já foi avisado."

# som -> (nome no painel, arquivo em assets/sons ou None)
OPCOES = {
    "residencial": ("Alarme residencial", "sirene_residencial.wav"),
    "alarme": ("Alarme contínuo", "sirene_alarme.wav"),
    "policia": ("Sirene de polícia", "sirene_policia.wav"),
    "emergencia": ("Sirene de emergência", "sirene_emergencia.wav"),
    "sintetizada": ("Sintetizada (gerada no PC)", None),
    "personalizado": ("Meu arquivo", None),
}
PADRAO = "residencial"
NIVEL_SIRENE, NIVEL_VOZ = 0.22, 0.30  # proporção voz/sirene (o limitador da voz come um pouco: sai ~1,05-1,1x)


def _tom_sintetizado(segundos=4.0) -> np.ndarray:
    """Sirene 'wail': frequência sobe e desce entre 650 e 1500 Hz a cada segundo."""
    t = np.arange(int(TAXA * segundos)) / TAXA
    freq = 650 + 850 * (0.5 - 0.5 * np.cos(2 * np.pi * t / 1.0))
    fase = 2 * np.pi * np.cumsum(freq) / TAXA
    return np.tanh(3 * np.sin(fase)).astype(np.float32)


def _ler_wav(caminho: Path) -> np.ndarray | None:
    """WAV 16 bits em qualquer taxa/canais -> mono float a TAXA."""
    try:
        with wave.open(str(caminho)) as w:
            if w.getsampwidth() != 2:
                return None
            taxa, canais = w.getframerate(), w.getnchannels()
            dados = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    except (OSError, wave.Error):
        return None
    if canais > 1:
        dados = dados.reshape(-1, canais).mean(axis=1)
    if taxa != TAXA and len(dados):
        x = np.arange(0, len(dados) * TAXA / taxa) * taxa / TAXA
        dados = np.interp(x, np.arange(len(dados)), dados).astype(np.float32)
    return dados if len(dados) else None


def _nivelar(x: np.ndarray, rms: float) -> np.ndarray:
    return x * (rms / (float(np.sqrt(np.mean(x ** 2))) or 1))


def _gerar_voz_windows(texto: str) -> bool:
    """Reserva: síntese de voz do Windows (soa robótica; só se a voz neural faltar)."""
    if sys.platform != "win32":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'pt-BR' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
        "$s.Rate = -1;"
        f"$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({TAXA}, 'Sixteen', 'Mono');"
        f"$s.SetOutputToWaveFile('{ARQ_VOZ_WINDOWS}', $fmt);"
        f"$s.Speak('{texto}'); $s.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ARQ_VOZ_WINDOWS.exists()


def _voz() -> np.ndarray | None:
    voz = _ler_wav(ARQ_VOZ_NEURAL)
    if voz is None and (ARQ_VOZ_WINDOWS.exists() or _gerar_voz_windows(MENSAGEM)):
        voz = _ler_wav(ARQ_VOZ_WINDOWS)
    if voz is None:
        return None
    voz = np.tanh(2.0 * voz / (np.max(np.abs(voz)) or 1))  # compressão leve: voz clara sobre o ruído
    return _nivelar(voz, NIVEL_VOZ)


def _sirene(som: str) -> np.ndarray:
    arquivo = OPCOES.get(som, OPCOES[PADRAO])[1]
    x = None
    if som == "personalizado":
        x = _ler_wav(ARQ_PERSONALIZADO)
    elif arquivo:
        x = _ler_wav(SONS / arquivo)
    if x is None:
        if som != "sintetizada":
            log.warning("Som da sirene '%s' indisponível; usando a sintetizada", som)
        x = _tom_sintetizado()
    if som != "personalizado":
        while len(x) < TAXA * 4:  # trechos curtos repetem até ~4 s antes da voz
            x = np.concatenate([x, x])
    return _nivelar(x, NIVEL_SIRENE)


def _o_mais_alto(x: np.ndarray) -> float:
    """Ganho que leva x ao máximo sem distorcer: o pico (percentil 99,9) chega a 0,95."""
    return 0.95 / (float(np.percentile(np.abs(x), 99.9)) or 1)


def montar(som: str, voz: bool) -> np.ndarray:
    """Uma volta do loop: sirene (+ voz + sirene), o mais alto possível.

    A sirene sobe até o pico máximo (com voz, até RMS 0,36, para a voz conseguir acompanhar).
    A voz fica na mesma proporção sobre a sirene (NIVEL_VOZ / NIVEL_SIRENE), com os picos
    arredondados por um limitador suave em vez de cortados."""
    s = _sirene(som)
    v = _voz() if voz else None
    rms = lambda x: float(np.sqrt(np.mean(x ** 2))) or 1  # noqa: E731
    ganho = _o_mais_alto(s) if v is None else min(_o_mais_alto(s), 0.36 / rms(s))
    s = s * ganho
    partes = [s]
    if v is not None:
        v = v * (rms(s) * NIVEL_VOZ / NIVEL_SIRENE / rms(v))
        v = 0.98 * np.tanh(v / 0.98)
        pausa = np.zeros(int(TAXA * 0.35), np.float32)
        partes += [pausa, v, pausa, s]
    return np.clip(np.concatenate(partes), -0.98, 0.98).astype(np.float32)


def wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def preparar_audio(som: str = PADRAO, voz: bool = True) -> Path:
    """Grava a volta do loop num arquivo próprio para (som, voz): trocar de som não
    mexe no arquivo que pode estar tocando agora."""
    PASTA.mkdir(exist_ok=True)
    destino = PASTA / f"sirene_{som}{'_voz' if voz else ''}.wav"
    destino.write_bytes(wav_bytes(montar(som, voz)))
    return destino


def salvar_personalizado(dados: bytes, max_s: int = 60) -> float:
    """Converte qualquer áudio enviado (mp3, wav, m4a, ogg...) com o ffmpeg. Devolve a duração."""
    PASTA.mkdir(exist_ok=True)
    tmp = PASTA / "sirene_envio.tmp"
    tmp.write_bytes(dados)
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp), "-t", str(max_s), "-ac", "1",
                            "-ar", str(TAXA), "-c:a", "pcm_s16le", str(ARQ_PERSONALIZADO)],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Não foi possível converter o arquivo (o FFmpeg está instalado?).")
    finally:
        tmp.unlink(missing_ok=True)
    x = _ler_wav(ARQ_PERSONALIZADO) if r.returncode == 0 else None
    if x is None or len(x) < TAXA * 0.5:
        ARQ_PERSONALIZADO.unlink(missing_ok=True)
        raise ValueError("Arquivo de áudio inválido ou curto demais (mínimo 0,5 s).")
    return len(x) / TAXA


class Sirene:
    """Toca a volta do loop por até `segundos`. Thread-safe."""

    def __init__(self, ao_mudar=None):
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.tocando = False
        self.desde = 0.0
        self.motivo = ""
        self.ao_mudar = ao_mudar  # callback(tocando: bool, motivo: str, quem: str)
        self.disponivel = sys.platform == "win32"
        self.arquivo: Path | None = None
        self._escolha = None

    def configurar(self, som: str, voz: bool, forcar=False):
        """Prepara o áudio do som escolhido (só refaz quando a escolha muda)."""
        if not self.disponivel or ((som, voz) == self._escolha and not forcar):
            return
        try:
            self.arquivo = preparar_audio(som, voz)
            self._escolha = (som, voz)
        except Exception:
            log.exception("Não foi possível preparar o som da sirene")

    def disparar(self, segundos: int, motivo: str) -> bool:
        if not self.disponivel:
            log.warning("Sirene indisponível neste sistema")
            return False
        if self.arquivo is None:
            self.configurar(PADRAO, True)
        import winsound

        with self._lock:
            ja = self.tocando
            winsound.PlaySound(str(self.arquivo), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
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
                "ha_s": round(time.time() - self.desde) if self.tocando else 0, "motivo": self.motivo,
                "personalizado": ARQ_PERSONALIZADO.exists()}
