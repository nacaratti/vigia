"""API HTTP + painel web. Escuta só em 127.0.0.1 por padrão."""
import asyncio
import base64
import hmac
import logging
import os

import cv2
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod, descoberta, notificador
from . import sirene as sirene_mod
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

    def _primeira_vez() -> bool:
        """Gravador ainda não configurado: o painel abre o assistente de instalação."""
        c = cfgmod.credenciais()
        return not sistema.modo_teste and (not c["host"] or c["password"] in ("", "troque-me"))

    @app.get("/api/config")
    def obter_config():
        return {"config": sistema.cfg, "modo_teste": sistema.modo_teste, "dias": cfgmod.DIAS,
                "primeira_vez": _primeira_vez()}

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

    @app.post("/api/sirene/testar")
    def sirene_testar():
        if not sistema.sirene.disparar(3, "teste pelo painel"):
            raise HTTPException(503, "sirene indisponível neste sistema")
        return {"ok": True}

    @app.get("/api/sirene/sons")
    def sirene_sons():
        return {"sons": [{"id": k, "nome": n} for k, (n, _) in sirene_mod.OPCOES.items()],
                "personalizado": sirene_mod.ARQ_PERSONALIZADO.exists()}

    @app.get("/api/sirene/previa")
    def sirene_previa(som: str, voz: bool = False):
        """Uma volta do som para ouvir no navegador (não toca nas caixas do PC)."""
        if som not in sirene_mod.OPCOES:
            raise HTTPException(404)
        if som == "personalizado" and not sirene_mod.ARQ_PERSONALIZADO.exists():
            raise HTTPException(404, "nenhum arquivo enviado")
        return Response(sirene_mod.wav_bytes(sirene_mod.montar(som, voz)), media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/sirene/arquivo")
    async def sirene_arquivo(request: Request):
        """Corpo = o arquivo de áudio (até 15 MB). Vira o som 'Meu arquivo'."""
        dados = await request.body()
        if not dados or len(dados) > 15 * 2**20:
            raise HTTPException(400, "Envie um arquivo de áudio de até 15 MB.")
        try:
            dur = await asyncio.to_thread(sirene_mod.salvar_personalizado, dados)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if sistema.cfg["sirene"]["som"] == "personalizado":
            await asyncio.to_thread(sistema.sirene.configurar, "personalizado", sistema.cfg["sirene"]["voz"], True)
        logging.getLogger("sirene").info("Som personalizado da sirene enviado pelo painel (%.1f s)", dur)
        return {"duracao_s": round(dur, 1)}

    @app.post("/api/sirene/parar")
    def sirene_parar():
        sistema.sirene.parar(quem="painel")
        return {"ok": True}

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

    # --- busca de câmeras (só no próprio PC: envolve a senha do gravador) --------

    busca = {"atual": None}

    def _so_local(request: Request):
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "A busca de câmeras só pode ser feita no próprio PC do Vigia.")

    def _busca_ou_404() -> descoberta.BuscaCanais:
        if busca["atual"] is None:
            raise HTTPException(404, "nenhuma busca em andamento")
        return busca["atual"]

    @app.get("/api/descoberta")
    def descoberta_info(request: Request):
        _so_local(request)
        c = cfgmod.credenciais()
        return {"host": c["host"], "porta": int(c["port"] or 554), "usuario": c["user"],
                "tem_senha": bool(c["password"]) and c["password"] != "troque-me",
                "busca": busca["atual"].estado() if busca["atual"] else None}

    @app.post("/api/descoberta/aparelhos")
    async def descoberta_aparelhos(request: Request):
        """{"rede": "192.168.1.0/24" (opcional)} -> aparelhos com vídeo encontrados."""
        _so_local(request)
        extras = []
        try:
            if rede := str((await request.json()).get("rede") or "").strip():
                extras.append(descoberta.rede_permitida(rede))
            if host := cfgmod.credenciais()["host"]:  # gravador já configurado (pode estar atrás de outro roteador)
                extras.append(descoberta.rede_permitida(f"{host}/24"))
        except ValueError as e:
            if rede:
                raise HTTPException(400, str(e))
        aparelhos, redes = await asyncio.to_thread(descoberta.buscar_aparelhos, extras)
        return {"aparelhos": aparelhos, "redes": [str(r) for r in redes]}

    @app.post("/api/descoberta/canais")
    async def descoberta_canais(request: Request):
        """{"host", "porta", "usuario", "senha"}: senha vazia = a do .env (mesmo aparelho)."""
        _so_local(request)
        if busca["atual"] is not None and not busca["atual"].fim:
            raise HTTPException(409, "já há uma busca em andamento")
        corpo = await request.json()
        host = str(corpo.get("host") or "").strip()
        try:
            if descoberta.rede_permitida(host).num_addresses != 1:
                raise ValueError("Informe o IP do aparelho.")
            porta = int(corpo.get("porta") or 554)
            if not 1 <= porta <= 65535:
                raise ValueError("Porta inválida.")
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        c = cfgmod.credenciais()
        usuario = str(corpo.get("usuario") or "").strip()
        senha = str(corpo.get("senha") or "")
        if not senha and host == c["host"] and usuario in ("", c["user"]):
            usuario, senha = c["user"], c["password"]
        if not usuario or not senha:
            raise HTTPException(400, "Informe usuário e senha do aparelho.")
        busca["atual"] = descoberta.BuscaCanais(host, porta, usuario, senha, mascarar=cfgmod.mascarar)
        busca["atual"].thread.start()
        return busca["atual"].estado()

    @app.get("/api/descoberta/canais")
    def descoberta_estado(request: Request):
        _so_local(request)
        return _busca_ou_404().estado()

    @app.get("/api/descoberta/miniatura/{canal}")
    def descoberta_miniatura(canal: int, request: Request):
        _so_local(request)
        jpg = _busca_ou_404().miniaturas.get(canal)
        if not jpg:
            raise HTTPException(404)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/descoberta/aplicar")
    async def descoberta_aplicar(request: Request):
        """{"cameras": [{"canal": 2, "nome": "Quintal"}, ...]}"""
        _so_local(request)
        b = _busca_ou_404()
        if not b.fim or b.erro or not b.modelo:
            raise HTTPException(409, "a busca não terminou com sucesso")
        escolhidas = (await request.json()).get("cameras")
        if not isinstance(escolhidas, list):
            raise HTTPException(400, "informe as câmeras")
        try:
            novo = await asyncio.to_thread(sistema.aplicar_descoberta, b, escolhidas)
        except (cfgmod.ConfigError, ValueError, TypeError, KeyError) as e:
            raise HTTPException(400, str(e))
        busca["atual"] = None  # não guarda a senha na memória além do necessário
        return {"config": novo}

    # --- assistente: Telegram (só no próprio PC: envolve o token do bot) ------------

    assist = {"token": None, "bot": None, "offset": None, "contatos": {}, "nomes": {}}

    def _mesmo_bot_escutando() -> bool:
        return assist["token"] == sistema.telegram.token and sistema.telegram.escutando

    def _nomes_atuais() -> dict[str, str]:
        """Nome de cada destinatário já configurado (cache; o .env só guarda os números)."""
        tg = sistema.telegram
        for cid in tg.chat_ids:
            if cid not in assist["nomes"]:
                try:
                    r = requests.get(notificador.API.format(token=tg.token, metodo="getChat"),
                                     params={"chat_id": cid}, timeout=10)
                    assist["nomes"][cid] = notificador.nome_chat(r.json().get("result") or {}) if r.ok else "?"
                except (requests.RequestException, ValueError):
                    return {c: assist["nomes"].get(c, "?") for c in tg.chat_ids}
        return {c: assist["nomes"][c] for c in tg.chat_ids}

    @app.get("/api/assistente")
    async def assistente_info(request: Request):
        _so_local(request)
        c, tg = cfgmod.credenciais(), sistema.telegram
        bot = None
        if tg.token:
            try:
                bot = (await asyncio.to_thread(notificador.info_bot, tg.token))["usuario"]
            except (ValueError, requests.RequestException):
                pass
        return {"primeira_vez": _primeira_vez(), "modo_teste": sistema.modo_teste,
                "gravador": {"host": c["host"], "cameras": sum(1 for x in sistema.cfg["cameras"] if not x.get("fonte"))},
                "telegram": {"bot": bot, "destinatarios": len(tg.chat_ids), "ligado": sistema.cfg["alertas"]["telegram"]}}

    @app.post("/api/assistente/telegram/bot")
    async def assistente_bot(request: Request):
        """{"token": "..."}: vazio = o bot já configurado."""
        _so_local(request)
        token = str((await request.json()).get("token") or "").strip() or sistema.telegram.token
        if not token:
            raise HTTPException(400, "Cole o token que o @BotFather mandou.")
        try:
            bot = await asyncio.to_thread(notificador.info_bot, token)
        except ValueError as e:
            raise HTTPException(400, str(e))
        assist.update(token=token, bot=bot, offset=None, contatos={})
        return {"usuario": bot["usuario"], "nome": bot["nome"], "link": f"https://t.me/{bot['usuario']}",
                "mesmo_bot": token == sistema.telegram.token}

    @app.get("/api/assistente/telegram/contatos")
    async def assistente_contatos(request: Request):
        """Quem já recebe os alertas + quem mandou mensagem ao bot (para autorizar)."""
        _so_local(request)
        if not assist["token"]:
            raise HTTPException(404, "conecte o bot primeiro")
        if _mesmo_bot_escutando():
            assist["contatos"].update(sistema.telegram.desconhecidos)
        else:
            try:
                novos, assist["offset"] = await asyncio.to_thread(notificador.ler_contatos, assist["token"], assist["offset"])
            except ValueError as e:
                raise HTTPException(409, str(e))
            except requests.RequestException:
                raise HTTPException(503, "sem conexão com o Telegram")
            assist["contatos"].update(novos)
        atuais = await asyncio.to_thread(_nomes_atuais) if assist["token"] == sistema.telegram.token else {}
        itens = [{"id": cid, "nome": nome, "autorizado": True} for cid, nome in atuais.items()]
        itens += [{"id": cid, "nome": nome, "autorizado": False} for cid, nome in assist["contatos"].items() if cid not in atuais]
        return {"contatos": itens}

    @app.post("/api/assistente/telegram/salvar")
    async def assistente_salvar(request: Request):
        """{"chat_ids": ["111", ...]}: a lista completa de quem recebe os alertas."""
        _so_local(request)
        if not assist["token"]:
            raise HTTPException(404, "conecte o bot primeiro")
        ids = [str(i).strip() for i in (await request.json()).get("chat_ids") or []]
        conhecidos = set(assist["contatos"]) | (set(sistema.telegram.chat_ids) if assist["token"] == sistema.telegram.token else set())
        if not ids:
            raise HTTPException(400, "Escolha pelo menos uma pessoa para receber os alertas.")
        if any(i not in conhecidos for i in ids):
            raise HTTPException(400, "Só é possível escolher quem já mandou mensagem ao bot.")
        novos = await asyncio.to_thread(sistema.configurar_telegram, assist["token"], list(dict.fromkeys(ids)))
        assist.update(token=None, offset=None, contatos={})
        return {"destinatarios": len(ids), "novos": len(novos), "config": sistema.cfg}

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
