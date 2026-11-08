#!/usr/bin/env python3
"""
telegram_bot.py — Lightweight Telegram remote for omfplays.
Just logs and status. No heavy build/upload buttons.
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
except ImportError:
    import subprocess as _sp
    _sp.run([sys.executable, "-m", "pip", "install", "python-telegram-bot[job-queue]", "--break-system-packages"], check=True)
    from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
LOGS_DIR = HERE / "logs"
PIPELINE_LOG = LOGS_DIR / "pipeline.log"
UPLOADS_PATH = STATE_DIR / "uploads.json"
SHORTS_DIR = HERE / "shorts"
RAW_DIR = HERE / "raw"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = set(os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")) if os.getenv("TELEGRAM_ALLOWED_USERS") else set()


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS


def build_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📊 Status"), KeyboardButton("📜 Logs")],
        [KeyboardButton("🎬 Shorts"), KeyboardButton("📂 Raw")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def status_text() -> str:
    try:
        import pipeline_status as ps
        snap = ps.snapshot()
        lines = [
            f"🕐 {snap['now_ist']}",
            f"🖥  node: {snap['node'].upper()}",
            f"📅 slots: {snap['shorts']['posted_today']}/{snap['shorts']['slots_total']}",
            f"🎬 shorts waiting: {snap['shorts']['pending']}",
            f"📦 manifest: {snap['manifest']['total']}",
            f"💾 disk: {snap['disk_free_gb']} GB free",
        ]
        raw = snap["raw"]
        lines += [
            f"📥 raw videos: {raw['total']}",
            f"🎞 reels left: {raw.get('slices_left', 0)}",
        ]
        if snap["attention"]:
            lines += ["", "⚠ " + "\n⚠ ".join(snap["attention"][:3])]
        return "\n".join(lines)
    except Exception as e:
        return f"Status error: {e}"


def logs_text() -> str:
    if not PIPELINE_LOG.exists():
        return "No pipeline.log found yet."
    lines = PIPELINE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-40:])


def shorts_text() -> str:
    uploads = load_json(UPLOADS_PATH, {})
    posted = {v.get("short") for v in uploads.values() if v.get("status") == "posted"}
    if not SHORTS_DIR.exists():
        return "No shorts folder."
    items = []
    for f in sorted(SHORTS_DIR.glob("s*.mp4")):
        icon = "✅" if f.stem in posted else "⏳"
        size_mb = f.stat().st_size / 1024 / 1024
        items.append(f"{icon} {f.stem} — {size_mb:.1f} MB")
    if not items:
        return "No shorts found."
    return "\n".join(items[:30])


def raw_text() -> str:
    if not RAW_DIR.exists():
        return "No raw folder."
    vids = [f for f in RAW_DIR.iterdir() if f.is_file() and f.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}]
    if not vids:
        return "Raw folder is empty."
    lines = [f"📥 {len(vids)} raw video(s):"]
    for v in vids[:15]:
        size_mb = v.stat().st_size / 1024 / 1024
        lines.append(f"  • {v.name} ({size_mb:.1f} MB)")
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 omfplays Remote\nPick what you want to see:",
        reply_markup=build_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"<pre>{status_text()}</pre>", parse_mode="HTML")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"<pre>{logs_text()}</pre>", parse_mode="HTML")


async def cmd_shorts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"<pre>{shorts_text()}</pre>", parse_mode="HTML")


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"<pre>{raw_text()}</pre>", parse_mode="HTML")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text = (update.message.text or "").strip()
    if text == "📊 Status":
        await cmd_status(update, context)
    elif text == "📜 Logs":
        await cmd_logs(update, context)
    elif text == "🎬 Shorts":
        await cmd_shorts(update, context)
    elif text == "📂 Raw":
        await cmd_raw(update, context)
    else:
        await update.message.reply_text("Use the buttons below.", reply_markup=build_keyboard())


def run_daemon():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", flush=True)
        sys.exit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("shorts", cmd_shorts))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Bot started. Press Ctrl+C to stop.", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        print("Usage: python telegram_bot.py --daemon")
