"""API HTTP + painel web. Escuta só em 127.0.0.1 por padrão."""
import asyncio
import base64
import hmac
import logging
import os

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod
from .logs import buffer
from .sistema import Sistema

WEB = cfgmod.RAIZ / "web"


def criar_app(sistema: Sistema, hosts_extras: tuple = ()) -> FastAPI:
    app = FastAPI(title="Monitoramento", docs_url=None, redoc_url=None, openapi_url=None)
    hosts_ok = {"127.0.0.1", "localhost", "[::1]", *hosts_extras} | {
        h.strip() for h in os.getenv("PAINEL_HOSTS_EXTRAS", "").split(",") if h.strip()
    }

    senha = os.getenv("PAINEL_SENHA", "")

    def autorizado(request: Request) -> bool:
        cab = request.headers.get("authorization", "")
        if not cab.lower().startswith("basic "):
            return False
        try:
            _, _, enviada = base64.b64decode(cab[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(enviada.encode(), senha.encode())

    @app.middleware("http")
    async def protecao(request: Request, call_next):
        # Contra DNS rebinding: só aceita Host conhecido.
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in hosts_ok:
            return JSONResponse({"erro": "host não permitido"}, status_code=403)
        # Senha do painel (PAINEL_SENHA no .env). O navegador pede usuário/senha; o usuário é ignorado.
        if senha and not autorizado(request):
            await asyncio.sleep(1)  # atrasa tentativas de adivinhar a senha
            return Response("Senha necessária", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Vigia", charset="UTF-8"'})
        # Contra CSRF: escrita exige cabeçalho próprio (sites de terceiros não conseguem enviá-lo).
        if request.method not in ("GET", "HEAD") and request.headers.get("x-painel") != "1":
            return JSONResponse({"erro": "requisição recusada"}, status_code=403)
        resposta = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Sempre confere se o painel mudou (senão o navegador mostra a versão antiga após atualizações).
            resposta.headers["Cache-Control"] = "no-cache"
        return resposta

    def _cam_ou_404(cid: str) -> dict:
        cam = sistema.camera(cid)
        if not cam:
            raise HTTPException(404, "câmera não encontrada")
        return cam

    def _jpeg(img, qualidade=80, largura=None) -> bytes:
        if largura and img.shape[1] > largura:
            img = cv2.resize(img, (largura, round(img.shape[0] * largura / img.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualidade])
        return buf.tobytes() if ok else b""

    # --- páginas ------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # --- configuração -------------------------------------------------------

    @app.get("/api/config")
    def obter_config():
        return {"config": sistema.cfg, "modo_teste": sistema.modo_teste, "dias": cfgmod.DIAS}

    @app.put("/api/config")
    async def salvar_config(request: Request):
        try:
            novo = await asyncio.to_thread(sistema.aplicar_config, await request.json())
        except cfgmod.ConfigError as e:
            raise HTTPException(400, str(e))
        return {"config": novo}

    # --- estado -------------------------------------------------------------

    @app.get("/api/status")
    def status():
        return sistema.status()

    @app.post("/api/armar")
    async def armar(request: Request):
        sistema.definir_armado(bool((await request.json()).get("armado")), "manual")
        return {"armado": sistema.armado}

    @app.post("/api/cameras/{cid}/ativo")
    async def camera_ativa(cid: str, request: Request):
        _cam_ou_404(cid)
        ativo = bool((await request.json()).get("ativo"))
        cfg = await asyncio.to_thread(sistema.definir_camera_ativa, cid, ativo)
        return {"config": cfg}

    @app.get("/api/logs")
    def logs(n: int = 150):
        return list(buffer.linhas)[-min(n, 300):]

    @app.post("/api/telegram/teste")
    async def telegram_teste():
        ok, msg = await asyncio.to_thread(sistema.telegram.enviar_texto, "✅ Teste do sistema de monitoramento")
        return {"ok": ok, "mensagem": msg}

    # --- vídeo --------------------------------------------------------------

    @app.get("/api/cameras/{cid}/snapshot")
    def snapshot(cid: str, anotado: bool = False, largura: int | None = None):
        """Imagem atual. O ao vivo do painel pede uma destas a cada ~0,2 s:
        requisições curtas não esgotam o limite de ~6 conexões do navegador,
        o que acontecia com um stream MJPEG aberto por câmera."""
        _cam_ou_404(cid)
        img = sistema.frame_anotado(cid, anotar=anotado)
        if img is None:
            raise HTTPException(503, "sem imagem da câmera ainda")
        return Response(_jpeg(img, 70 if anotado else 85, largura or None), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/cameras/{cid}/testar")
    async def testar(cid: str):
        _cam_ou_404(cid)
        r = await asyncio.to_thread(sistema.testar_deteccao, cid)
        if r is None:
            raise HTTPException(503, "sem imagem ou modelo indisponível")
        img, dentro, fora = r
        return Response(_jpeg(img, 85), media_type="image/jpeg", headers={
            "X-Pessoas-Zona": str(len(dentro)), "X-Pessoas-Fora": str(len(fora)), "Cache-Control": "no-store",
        })

    # --- eventos ------------------------------------------------------------

    @app.get("/api/eventos")
    def eventos(camera: str | None = None, data: str | None = None, limite: int = 48, offset: int = 0):
        itens, total = sistema.eventos.listar(camera, data, min(limite, 200), max(offset, 0))
        return {"itens": itens, "total": total}

    @app.post("/api/eventos/apagar")
    async def apagar_eventos(request: Request):
        """{"ids": [...], "tipo": "video" | "evento"}"""
        corpo = await request.json()
        tipo = corpo.get("tipo")
        if tipo not in ("video", "evento") or not isinstance(corpo.get("ids"), list):
            raise HTTPException(400, "informe ids e tipo (video ou evento)")
        r = await asyncio.to_thread(sistema.eventos.apagar, corpo["ids"], tipo == "video")
        logging.getLogger("eventos").info("Apagados pelo painel: %s %s(s), %.1f MB liberados",
                                          r["apagados"], tipo, r["bytes"] / 2**20)
        return r

    @app.get("/api/armazenamento")
    def armazenamento():
        return sistema.eventos.armazenamento()

    @app.get("/api/eventos/{eid}/imagem")
    def imagem_evento(eid: int):
        ev = sistema.eventos.obter(eid)
        if not ev:
            raise HTTPException(404)
        caminho = (cfgmod.RAIZ / ev["imagem"]).resolve()
        if not caminho.is_relative_to(cfgmod.RAIZ / "snapshots") or not caminho.exists():
            raise HTTPException(404, "imagem não encontrada")
        return FileResponse(caminho, media_type="image/jpeg")

    @app.get("/api/eventos/{eid}/video")
    def video_evento(eid: int):
        ev = sistema.eventos.obter(eid)
        if not ev or not ev.get("video"):
            raise HTTPException(404, "evento sem vídeo")
        caminho = (cfgmod.RAIZ / ev["video"]).resolve()
        if not caminho.is_relative_to(cfgmod.RAIZ / "snapshots") or not caminho.exists():
            raise HTTPException(404, "vídeo não encontrado")
        return FileResponse(caminho, media_type="video/mp4")

    return app
