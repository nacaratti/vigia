"""Testa a conexão RTSP com o DVR/câmeras e salva um snapshot de cada canal.

Credenciais vêm do .env (CAM_HOST, CAM_RTSP_PORT, CAM_USER, CAM_PASSWORD).

Uso:
    python scripts/testar_rtsp.py              # testa canais 1..16 (subtype=1)
    python scripts/testar_rtsp.py --canais 4   # testa canais 1..4

Fluxo:
  1. Valida a senha UMA vez com ffprobe no canal 1. Se vier 401, aborta
     (DVRs Intelbras/Dahua bloqueiam o usuário após poucas tentativas erradas).
  2. Se a URL padrão falhar por outro motivo, testa variações comuns.
  3. Para cada canal, abre com OpenCV, lê frames e salva snapshots/canal_N.jpg.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import cv2
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

HOST = os.getenv("CAM_HOST", "")
PORTA = os.getenv("CAM_RTSP_PORT", "554")
USUARIO = os.getenv("CAM_USER", "")
SENHA = os.getenv("CAM_PASSWORD", "")

# Variações de caminho a testar se o padrão falhar. {c}=canal, {s}=subtype
VARIACOES = [
    ("padrão Dahua/Intelbras", "/cam/realmonitor?channel={c}&subtype={s}"),
    ("sem subtype", "/cam/realmonitor?channel={c}"),
    ("Intelbras antigo", "/user={u}&password={p}&channel={c}&stream={s}.sdp"),
    ("H264 genérico", "/h264/ch{c}/{tipo}/av_stream"),
]


def montar_url(caminho: str, canal: int, subtype: int, porta: str = PORTA) -> str:
    u, p = quote(USUARIO, safe=""), quote(SENHA, safe="")
    tipo = "sub" if subtype == 1 else "main"
    path = caminho.format(c=canal, s=subtype, u=u, p=p, tipo=tipo)
    if "user=" in caminho:  # credenciais no próprio caminho
        return f"rtsp://{HOST}:{porta}{path}"
    return f"rtsp://{u}:{p}@{HOST}:{porta}{path}"


def mascarar(texto: str) -> str:
    """Remove a senha (crua e codificada) de qualquer texto antes de imprimir."""
    for segredo in {SENHA, quote(SENHA, safe="")}:
        if segredo:
            texto = texto.replace(segredo, "****")
    return texto


def ffprobe(url: str, timeout: int = 12) -> tuple[str, str]:
    """Retorna (status, detalhe). status: ok | auth | notfound | erro."""
    if not shutil.which("ffprobe"):
        return "erro", "ffprobe não encontrado no PATH"
    cmd = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(timeout * 1_000_000),
        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
        "-of", "csv=p=0", url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return "erro", "timeout"
    saida = mascarar((r.stdout + r.stderr).strip())
    if "401" in saida or "Unauthorized" in saida:
        return "auth", saida
    if "404" in saida or "Not Found" in saida:
        return "notfound", saida
    if r.returncode == 0 and r.stdout.strip():
        return "ok", r.stdout.strip()
    return "erro", saida or f"código {r.returncode}"


def capturar_snapshot(url: str, destino: Path, n_frames: int = 15) -> tuple[bool, str]:
    cap = cv2.VideoCapture(
        url, cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000],
    )
    if not cap.isOpened():
        return False, "OpenCV não conseguiu abrir o stream"
    t0, lidos, frame = time.time(), 0, None
    for _ in range(n_frames):
        ok, f = cap.read()
        if ok:
            lidos, frame = lidos + 1, f
    cap.release()
    if frame is None:
        return False, "conectou mas não recebeu frames"
    fps = lidos / max(time.time() - t0, 1e-6)
    cv2.imwrite(str(destino), frame)
    h, w = frame.shape[:2]
    return True, f"{lidos}/{n_frames} frames, {w}x{h}, ~{fps:.1f} fps de leitura"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canais", type=int, default=16, help="testar canais 1..N (padrão 16)")
    args = ap.parse_args()

    if not (HOST and USUARIO and SENHA) or SENHA == "troque-me":
        sys.exit("Preencha CAM_HOST, CAM_USER e CAM_PASSWORD no arquivo .env (veja .env.example).")
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

    # 1) Validação única da senha + escolha do formato de URL
    print(f"DVR: {HOST}:{PORTA}  usuário: {USUARIO}")
    escolhido = None
    for nome, caminho in VARIACOES:
        for subtype in (1, 0):
            url = montar_url(caminho, 1, subtype)
            status, det = ffprobe(url)
            print(f"  [{status:8}] {nome}, subtype={subtype}: {det.splitlines()[0] if det else ''}")
            if status == "auth":
                sys.exit(
                    "\nSenha/usuário recusados (401). Parei para não bloquear o usuário no DVR.\n"
                    "Confira o .env (é a mesma senha usada no iSIC/web do DVR) e rode de novo."
                )
            if status == "ok":
                escolhido = (nome, caminho, subtype)
                break
        if escolhido:
            break
    if not escolhido:
        sys.exit("\nNenhuma variação funcionou no canal 1. Me mostre a saída acima.")

    nome, caminho, subtype = escolhido
    print(f"\nFormato que funcionou: {nome} (subtype={subtype})")
    print(f"  {mascarar(montar_url(caminho, 1, subtype))}\n")

    # 2) Varre os canais e salva snapshots
    pasta = RAIZ / "snapshots" / "teste"
    pasta.mkdir(parents=True, exist_ok=True)
    funcionando, falhas_seguidas = [], 0
    for canal in range(1, args.canais + 1):
        url = montar_url(caminho, canal, subtype)
        status, det = ffprobe(url)
        if status != "ok":
            print(f"Canal {canal:2}: sem vídeo ({status})")
            falhas_seguidas += 1
            if falhas_seguidas >= 3 and funcionando:
                print("3 canais seguidos sem vídeo: encerrando a varredura.")
                break
            continue
        falhas_seguidas = 0
        destino = pasta / f"canal_{canal}.jpg"
        ok, info = capturar_snapshot(url, destino)
        print(f"Canal {canal:2}: {'OK  ' if ok else 'FALHA'} {det} | {info}" + (f" -> {destino.relative_to(RAIZ)}" if ok else ""))
        if ok:
            funcionando.append(canal)

    print(f"\nCanais com vídeo: {funcionando or 'nenhum'}")


if __name__ == "__main__":
    main()
