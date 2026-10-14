"""Alertas via Telegram, enviados em segundo plano com novas tentativas."""
import logging
import os
import queue
import threading
import time
from pathlib import Path

import requests

from .config import mascarar

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{metodo}"


class Telegram:
    def __init__(self, ao_concluir=None, desativado=False):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        # Um ou mais destinatários, separados por vírgula: TELEGRAM_CHAT_ID=111,222
        self.chat_ids = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        self.desativado = desativado
        self.ao_concluir = ao_concluir  # callback(evento_id, status)
        self._fila: queue.Queue = queue.Queue(maxsize=100)
        self.ultimo_erro = ""
        self.enviados = 0
        threading.Thread(target=self._loop, name="telegram", daemon=True).start()

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
