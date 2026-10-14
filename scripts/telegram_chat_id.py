"""Descobre o TELEGRAM_CHAT_ID: mande qualquer mensagem para o seu bot e rode este script.

    python scripts/telegram_chat_id.py
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    sys.exit("Coloque TELEGRAM_BOT_TOKEN no .env primeiro (o token que o @BotFather te deu).")

try:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
except requests.RequestException:
    sys.exit("Erro de rede ao falar com o Telegram.")
if r.status_code == 401:
    sys.exit("Token recusado pelo Telegram. Confira o TELEGRAM_BOT_TOKEN.")

chats = {}
for u in r.json().get("result", []):
    msg = u.get("message") or u.get("channel_post") or {}
    chat = msg.get("chat")
    if chat:
        chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
if not chats:
    sys.exit("Nenhuma mensagem encontrada. Abra o seu bot no Telegram, mande um 'oi' e rode de novo.")
for cid, nome in chats.items():
    print(f"TELEGRAM_CHAT_ID={cid}   ({nome})")
print("\nCopie a linha acima para o .env.")
