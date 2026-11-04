#!/usr/bin/env python3
"""
debug_telegram.py — Send Telegram summary from GitHub Actions workflow.
Reads env vars for token/chat/mode/status and sends a formatted message.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODE = os.getenv("MODE", "debug")
STATUS = os.getenv("JOB_STATUS", "unknown")

if not BOT_TOKEN or not CHAT_ID:
    print("Telegram not configured")
    sys.exit(0)

icon = {"success": "✅", "failure": "❌", "cancelled": "⚠️"}.get(STATUS, "ℹ️")
text = (
    f"{icon} <b>Night Ops Complete</b>\n"
    f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
    f"⚙️ Mode: <code>{MODE}</code>\n"
    f"📊 Status: <b>{STATUS.upper()}</b>\n"
    f"🔗 <a href=\"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}\">View Run</a>"
)

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
payload = json.dumps(
    {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
).encode("utf-8")

try:
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status == 200:
            print("Telegram summary sent")
        else:
            print(f"Telegram returned status {resp.status}")
except Exception as e:
    print(f"Telegram send failed: {e}")
    sys.exit(1)