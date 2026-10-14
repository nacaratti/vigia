"""Atalho da área de trabalho: inicia o Vigia (se preciso) e abre o painel.

Executado com pythonw.exe, então não abre janela de terminal.
"""
import ctypes
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8000"
ESPERA_MAX_S = 120  # carregar o modelo e conectar as 4 câmeras leva ~30 s


def rodando() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/status", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def aviso(texto: str):
    ctypes.windll.user32.MessageBoxW(None, texto, "Vigia", 0x30)  # ícone de alerta


def main():
    if not rodando():
        pythonw = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            aviso(f"Ambiente Python não encontrado em:\n{pythonw}\n\nVeja a instalação no README.")
            return
        subprocess.Popen(
            [str(pythonw), "main.py"], cwd=RAIZ,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        inicio = time.time()
        while not rodando():
            if time.time() - inicio > ESPERA_MAX_S:
                aviso("O Vigia não iniciou.\n\nVeja o motivo no final do arquivo:\n"
                      f"{RAIZ / 'logs' / 'monitor.log'}")
                return
            time.sleep(1)
    webbrowser.open(URL)


if __name__ == "__main__":
    sys.exit(main())
