"""Logging com rotação de arquivo, máscara de segredos e buffer para o painel."""
import logging
import sys
from collections import deque
from logging.handlers import RotatingFileHandler

from .config import RAIZ, mascarar

FORMATO = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class FiltroSegredos(logging.Filter):
    def filter(self, record):
        record.msg = mascarar(record.getMessage())
        record.args = None
        return True


class FiltroDesconexao(logging.Filter):
    """No Windows, o asyncio registra como ERRO quando o navegador fecha uma
    conexão (vídeo ao vivo, player). É inofensivo; descartamos só esse caso."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (ConnectionResetError, ConnectionAbortedError))


class BufferMemoria(logging.Handler):
    """Mantém as últimas linhas para a aba Status do painel."""

    def __init__(self, tamanho=300):
        super().__init__()
        self.linhas: deque = deque(maxlen=tamanho)

    def emit(self, record):
        self.linhas.append({"ts": record.created, "nivel": record.levelname,
                            "origem": record.name, "msg": record.getMessage()})


buffer = BufferMemoria()


def configurar(nivel=logging.INFO):
    pasta = RAIZ / "logs"
    pasta.mkdir(exist_ok=True)
    arquivo = RotatingFileHandler(pasta / "monitor.log", maxBytes=5 * 2**20, backupCount=5, encoding="utf-8")
    handlers = [arquivo, buffer]
    if sys.stdout is not None:  # pythonw.exe (início automático) não tem console
        handlers.append(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter(FORMATO, "%Y-%m-%d %H:%M:%S")
    filtro = FiltroSegredos()
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(filtro)
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    raiz.handlers[:] = handlers
    logging.getLogger("asyncio").addFilter(FiltroDesconexao())
    for ruidoso in ("ultralytics", "uvicorn.access", "httpx"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
