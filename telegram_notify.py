#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    cfg["telegram_bot_token"] = os.environ.get(
        "TELEGRAM_BOT_TOKEN", cfg.get("telegram_bot_token", ""))
    cfg["telegram_chat_id"] = os.environ.get(
        "TELEGRAM_CHAT_ID", cfg.get("telegram_chat_id", ""))
    return cfg


def send_telegram(message):
    cfg = load_config()
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not token or token == "YOUR_BOT_TOKEN" or not chat_id or chat_id == "YOUR_CHAT_ID":
        print("Telegram not configured; notification skipped.", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        timeout = int(cfg.get("telegram_timeout_seconds", 30))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as exc:
        print(f"Telegram notification failed: {exc}", file=sys.stderr)
        return False


def notify_master_report(date_str, daily_count, downloaded, built, uploaded, batch_dir, errors=None):
    lines = [
        f"\U0001f4ca <b>Daily Pipeline Report</b>",
        f"\U0001f4c5 {date_str}",
        "",
        f"\u2b07\ufe0f Downloaded: {downloaded} videos",
        f"\u2702\ufe0f Shorts Built: {built} clips",
        f"\u2b06\ufe0f Uploaded: {uploaded}/{daily_count} shorts",
        f"\U0001f4c1 Batch: {escape(str(batch_dir))}",
    ]
    if errors:
        lines.append("")
        lines.append(f"\u274c Errors: {escape(str(errors))}")
    status = "\u2705 Complete" if uploaded == daily_count and not errors else "\u26a0\ufe0f Incomplete"
    lines.insert(1, f"Status: {status}")
    msg = "\n".join(lines)
    return send_telegram(msg)


def notify_error(step, error_msg):
    msg = (f"\u274c <b>Pipeline Error</b>\n"
           f"Step: {escape(str(step))}\n"
           f"Error: {escape(str(error_msg))}")
    return send_telegram(msg)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        send_telegram(escape(" ".join(sys.argv[1:])))
    else:
        send_telegram("Test notification from contentfarming pipeline")

