"""Alertas via Telegram, enviados em segundo plano com novas tentativas."""
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

import requests

from .config import mascarar

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{metodo}"
TOKEN_VALIDO = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,50}$")


def nome_chat(chat: dict) -> str:
    return " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("title") or "?"


def info_bot(token: str) -> dict:
    """Confere o token com o Telegram: {"usuario": "meu_bot", "nome": "Meu Bot"}."""
    if not TOKEN_VALIDO.match(token):
        raise ValueError("Isso não parece um token de bot. Copie a linha inteira que o @BotFather mandou (números:letras).")
    try:
        r = requests.get(API.format(token=token, metodo="getMe"), timeout=15)
    except requests.RequestException:
        raise ValueError("Sem conexão com o Telegram. Verifique a internet do PC.")
    if r.status_code in (401, 404):
        raise ValueError("O Telegram recusou este token. Copie de novo o que o @BotFather mandou (ou gere outro com /token).")
    r.raise_for_status()
    res = r.json()["result"]
    return {"usuario": res.get("username", ""), "nome": res.get("first_name", "")}


def ler_contatos(token: str, offset: int | None) -> tuple[dict[str, str], int | None]:
    """Conversas particulares que escreveram ao bot desde `offset`: ({chat_id: nome}, novo offset).
    Só usar quando o Vigia NÃO está escutando este mesmo bot (o Telegram aceita um leitor por vez)."""
    r = requests.get(API.format(token=token, metodo="getUpdates"),
                     params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'}, timeout=15)
    if r.status_code == 409:
        raise ValueError("Este bot está ligado a outro programa (webhook ou outro leitor). Use um bot só para o Vigia.")
    r.raise_for_status()
    contatos = {}
    for u in r.json().get("result", []):
        offset = u["update_id"] + 1
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            contatos[str(chat["id"])] = nome_chat(chat)
    return contatos, offset


class Telegram:
    def __init__(self, ao_concluir=None, desativado=False):
        self.recarregar()
        self.desativado = desativado
        self.escutando = False
        # Quem escreveu ao bot sem estar autorizado (chat_id -> nome): o assistente mostra para autorizar.
        self.desconhecidos: dict[str, str] = {}
        self.ao_concluir = ao_concluir  # callback(evento_id, status)
        self._fila: queue.Queue = queue.Queue(maxsize=100)
        self.ultimo_erro = ""
        self.enviados = 0
        threading.Thread(target=self._loop, name="telegram", daemon=True).start()

    def recarregar(self):
        """Lê token e destinatários do .env (ou do ambiente já atualizado pelo assistente)."""
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        # Um ou mais destinatários, separados por vírgula: TELEGRAM_CHAT_ID=111,222
        self.chat_ids = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

    @property
    def configurado(self) -> bool:
        return bool(self.token and self.chat_ids)

    @property
    def ativo(self) -> bool:
        return self.configurado and not self.desativado

    def enviar_foto(self, evento_id: int, legenda: str, imagem: Path) -> str:
        """Enfileira; devolve o status inicial do alerta."""
        if not self.ativo:
            return "desativado"
        try:
            self._fila.put_nowait((evento_id, legenda, imagem))
            return "pendente"
        except queue.Full:
            log.error("Fila de alertas cheia; alerta do evento %s descartado", evento_id)
            return "falhou"

    def enviar_texto(self, texto: str) -> tuple[bool, str]:
        """Envio síncrono para todos os destinatários (botão de teste do painel)."""
        if not self.configurado:
            return False, "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID não estão no .env"
        falhas = []
        for chat_id in self.chat_ids:
            try:
                r = requests.post(API.format(token=self.token, metodo="sendMessage"),
                                  data={"chat_id": chat_id, "text": texto}, timeout=15)
                if not r.ok:
                    falhas.append(mascarar(f"{chat_id}: Telegram respondeu {r.status_code}: {r.text[:150]}"))
            except requests.RequestException as e:
                falhas.append(mascarar(f"{chat_id}: erro de rede: {e}"))
        if falhas:
            return False, "; ".join(falhas)
        n = len(self.chat_ids)
        return True, f"mensagem enviada para {n} destinatário{'s' if n > 1 else ''}"

    def _enviar_foto_para(self, chat_id: str, legenda: str, imagem: Path) -> bool:
        for tentativa in range(1, 4):
            try:
                with open(imagem, "rb") as f:
                    r = requests.post(
                        API.format(token=self.token, metodo="sendPhoto"),
                        data={"chat_id": chat_id, "caption": legenda},
                        files={"photo": f}, timeout=30,
                    )
                if r.ok:
                    return True
                self.ultimo_erro = mascarar(f"{chat_id}: HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code in (400, 401, 403, 404):  # erro de configuração: não adianta repetir
                    break
            except (requests.RequestException, OSError) as e:
                self.ultimo_erro = mascarar(f"{chat_id}: {e}")
            log.warning("Falha ao enviar alerta (tentativa %s/3): %s", tentativa, self.ultimo_erro)
            time.sleep(5 * tentativa)
        return False

    def _loop(self):
        while True:
            evento_id, legenda, imagem = self._fila.get()
            ok = [self._enviar_foto_para(c, legenda, imagem) for c in self.chat_ids]
            # "enviado" se chegou a pelo menos um destinatário; falhas parciais ficam no log
            status = "enviado" if any(ok) else "falhou"
            if any(ok):
                self.enviados += 1
                if all(ok):
                    self.ultimo_erro = ""
            if not all(ok):
                log.error("Alerta do evento %s não chegou a %s de %s destinatário(s): %s",
                          evento_id, ok.count(False), len(ok), self.ultimo_erro)
            if self.ao_concluir:
                self.ao_concluir(evento_id, status)

    # --- mensagens com botões e comandos recebidos ---------------------------

    @staticmethod
    def _teclado(botoes, por_linha=2) -> str:
        import json
        linhas = [botoes[i:i + por_linha] for i in range(0, len(botoes), por_linha)]
        return json.dumps({"inline_keyboard": [[{"text": r, "callback_data": a} for r, a in l] for l in linhas]})

    def enviar_aviso(self, texto: str, botoes: list[tuple[str, str]] | None = None, excluir: str | None = None,
                     apenas: list[str] | None = None):
        """Envia texto (com botões opcionais: [(rótulo, ação)]) a todos, em segundo plano."""
        if not self.ativo:
            return
        dados = {"text": texto}
        if botoes:
            dados["reply_markup"] = self._teclado(botoes)

        def enviar():
            for chat_id in self.chat_ids:
                if chat_id == excluir or (apenas is not None and chat_id not in apenas):
                    continue
                try:
                    requests.post(API.format(token=self.token, metodo="sendMessage"),
                                  data={**dados, "chat_id": chat_id}, timeout=15)
                except requests.RequestException as e:
                    log.warning("Falha ao enviar aviso: %s", mascarar(str(e)))

        threading.Thread(target=enviar, daemon=True).start()

    def enviar_fotos_para(self, chat_id: str, fotos: list[tuple[str, bytes]]) -> bool:
        """Envia JPEGs (legenda, bytes) a um destinatário; 2 a 10 viram um álbum."""
        try:
            if len(fotos) == 1:
                legenda, jpg = fotos[0]
                r = requests.post(API.format(token=self.token, metodo="sendPhoto"),
                                  data={"chat_id": chat_id, "caption": legenda},
                                  files={"photo": ("foto.jpg", jpg, "image/jpeg")}, timeout=60)
            else:
                import json
                midia = [{"type": "photo", "media": f"attach://f{i}", "caption": legenda}
                         for i, (legenda, _) in enumerate(fotos[:10])]
                r = requests.post(API.format(token=self.token, metodo="sendMediaGroup"),
                                  data={"chat_id": chat_id, "media": json.dumps(midia)},
                                  files={f"f{i}": (f"f{i}.jpg", jpg, "image/jpeg") for i, (_, jpg) in enumerate(fotos[:10])},
                                  timeout=90)
            if not r.ok:
                log.warning("Falha ao enviar fotos: HTTP %s: %s", r.status_code, mascarar(r.text[:200]))
            return r.ok
        except requests.RequestException as e:
            log.warning("Falha ao enviar fotos: %s", mascarar(str(e)))
            return False

    def escutar(self, comandos: dict):
        """Recebe cliques em botões e comandos de texto (ex.: /parar).

        `comandos` mapeia ação -> função(quem, chat_id, arg) -> str | (str, botões).
        `arg` é o resto do comando ("/ver 2" -> "2") ou "" quando não há.
        Só aceita mensagens dos chat ids do .env; o resto é ignorado e registrado no log.
        """
        if not self.ativo or self.escutando:
            return
        self.escutando = True
        threading.Thread(target=self._escutar, args=(comandos,), name="telegram-escuta", daemon=True).start()

    def registrar_comandos(self, lista: list[tuple[str, str]]):
        """Lista que o Telegram sugere ao digitar '/'."""
        if not self.ativo:
            return
        import json
        try:
            requests.post(API.format(token=self.token, metodo="setMyCommands"),
                          data={"commands": json.dumps([{"command": c, "description": d} for c, d in lista])}, timeout=15)
        except requests.RequestException as e:
            log.warning("Não foi possível registrar os comandos do bot: %s", mascarar(str(e)))

    def _escutar(self, comandos: dict):
        token, offset = None, None
        while True:
            if token != self.token:  # bot trocado pelo assistente
                token, offset = self.token, None
                url = API.format(token=token, metodo="getUpdates")
                try:  # ignora comandos antigos acumulados enquanto o Vigia estava fechado
                    r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=15).json()
                    if r.get("result"):
                        offset = r["result"][-1]["update_id"] + 1
                except (requests.RequestException, ValueError):
                    pass
            try:
                r = requests.get(url, params={"offset": offset, "timeout": 25,
                                              "allowed_updates": '["message","callback_query"]'}, timeout=35).json()
            except (requests.RequestException, ValueError) as e:
                log.debug("Escuta do Telegram: %s", mascarar(str(e)))
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    self._tratar(u, comandos)
                except Exception:
                    log.exception("Erro ao tratar comando do Telegram")

    def _tratar(self, u: dict, comandos: dict):
        cb = u.get("callback_query")
        msg = cb["message"] if cb else u.get("message") or {}
        remetente = (cb or msg).get("from", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id not in self.chat_ids or str(remetente.get("id", "")) not in self.chat_ids:
            log.warning("Comando ignorado de chat não autorizado (%s)", chat_id or "?")
            if msg.get("chat", {}).get("type") == "private" and len(self.desconhecidos) < 50:
                self.desconhecidos[chat_id] = nome_chat(msg["chat"])
            return
        partes = (cb.get("data", "") if cb else (msg.get("text") or "").strip().lstrip("/")).split(maxsplit=1)
        acao = partes[0].split("@")[0].lower() if partes else ""
        arg = partes[1].strip() if len(partes) > 1 else ""
        quem = f"Telegram ({remetente.get('first_name', '?')})"
        if cb:  # tira o "carregando" do botão (antes, porque enviar fotos demora)
            requests.post(API.format(token=self.token, metodo="answerCallbackQuery"),
                          data={"callback_query_id": cb["id"]}, timeout=15)
        funcao = comandos.get(acao) or comandos.get("menu")
        resposta = funcao(quem, chat_id, arg) if funcao else "Comandos: /armar /desarmar /status /parar"
        texto, botoes = resposta if isinstance(resposta, tuple) else (resposta, None)
        dados = {"chat_id": chat_id, "text": texto}
        if botoes:
            dados["reply_markup"] = self._teclado(botoes)
        requests.post(API.format(token=self.token, metodo="sendMessage"), data=dados, timeout=15)
