"""Ponto de entrada: inicia o monitoramento e o painel web.

    python main.py                         # câmeras reais (config.yaml + .env)
    python main.py --video teste.mp4       # modo teste: roda o pipeline sobre um vídeo local
    python main.py --video teste.mp4 --sem-alerta
"""
import argparse
import ipaddress
import logging
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn

from monitor import logs
from monitor.app import criar_app
from monitor.config import ConfigError
from monitor.sistema import Sistema


def manter_pc_acordado(log) -> bool:
    """Pede ao Windows para não suspender enquanto o Vigia estiver aberto.

    Não altera o plano de energia: a tela continua desligando, e ao fechar o
    Vigia o PC volta a suspender normalmente. Vale enquanto a thread principal
    (a do servidor) estiver viva.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) == 0:
        log.warning("Não foi possível impedir a suspensão do PC")
        return False
    log.info("Suspensão do PC bloqueada enquanto o Vigia estiver aberto (a tela pode desligar)")
    return True


def main():
    ap = argparse.ArgumentParser(description="Monitoramento de câmeras com detecção de pessoas")
    ap.add_argument("--video", help="arquivo de vídeo para o modo teste (ignora as câmeras e os horários)")
    ap.add_argument("--sem-alerta", action="store_true", help="não envia mensagens no Telegram")
    ap.add_argument("--ignorar-horario", action="store_true", help="já inicia armado, mesmo fora do horário automático")
    ap.add_argument("--host", default="127.0.0.1", help="endereço do painel (padrão: só este PC)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--abrir", action="store_true", help="abre o painel no navegador")
    ap.add_argument("--permitir-suspensao", action="store_true", help="não impede o PC de suspender")
    args = ap.parse_args()

    logs.configurar()
    log = logging.getLogger("main")

    if args.video and not Path(args.video).is_file():
        sys.exit(f"Vídeo não encontrado: {args.video}")
    try:
        ip = ipaddress.ip_address(args.host)
        if not (ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")):
            sys.exit("Recusado: o painel só pode escutar em loopback, rede privada ou Tailscale.")
        if not ip.is_loopback:
            if len(os.getenv("PAINEL_SENHA", "")) < 10:
                sys.exit("Para abrir o painel fora deste PC, defina PAINEL_SENHA no .env (mínimo 10 caracteres).")
            log.warning("Painel acessível por outros dispositivos em %s (com senha). Não faça redirecionamento de portas no roteador.", args.host)
    except ValueError:
        sys.exit("--host deve ser um IP (ex.: 127.0.0.1)")

    try:
        sistema = Sistema(video_teste=args.video, sem_alerta=args.sem_alerta)
    except (ConfigError, OSError) as e:
        sys.exit(f"Erro no config.yaml: {e}")
    if args.ignorar_horario:
        sistema.ignorar_horario = True
        log.warning("Iniciando armado, mesmo fora do horário automático")
    sistema.iniciar()
    if not args.permitir_suspensao and not args.video:
        manter_pc_acordado(log)

    url = f"http://127.0.0.1:{args.port}" if ipaddress.ip_address(args.host).is_loopback else f"http://{args.host}:{args.port}"
    log.info("Painel em %s", url)
    if args.abrir:
        webbrowser.open(url)
    try:
        uvicorn.run(criar_app(sistema, (args.host,)), host=args.host, port=args.port, log_level="warning", log_config=None)
    finally:
        sistema.parar()


if __name__ == "__main__":
    main()
