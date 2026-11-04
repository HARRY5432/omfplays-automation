#!/usr/bin/env python3
"""
telegram_bot.py — Interactive Telegram ops bot for omfplays.

Commands:
  /status      — Pipeline status (shorts count, posted, pending)
  /shorts      — List all shorts with status
  /cleanup     — Delete posted shorts from disk
  /logs        — Recent workflow logs
  /help        — Show commands

Runs as: python telegram_bot.py --daemon  (long-polling)
Or:      python telegram_bot.py --status  (one-shot for workflow)
"""
import os
import json
import sys
import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
except ImportError:
    print("Installing python-telegram-bot...")
    subprocess.run([sys.executable, "-m", "pip", "install", "python-telegram-bot[job-queue]", "--break-system-packages"], check=True)
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"
WORKFLOW_LOGS_DIR = HERE / ".github" / "workflows"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALLOWED_USERS = set(os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")) if os.getenv("TELEGRAM_ALLOWED_USERS") else set()

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def format_bytes(b):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def get_shorts_status():
    """Return list of shorts with upload status."""
    uploads = load_json(UPLOADS_PATH, {})
    posted = {v.get("short") for v in uploads.values() if v.get("status") == "posted"}
    
    shorts = []
    for f in sorted(SHORTS_DIR.glob("s*.mp4")):
        stem = f.stem
        size = f.stat().st_size
        status = "✅ Posted" if stem in posted else "⏳ Pending"
        shorts.append({"name": stem, "size": size, "status": status, "posted": stem in posted})
    return shorts

def get_disk_usage():
    total = used = free = 0
    try:
        import shutil
        total, used, free = shutil.disk_usage(HERE)
    except:
        pass
    return total, used, free

async def send_message(text: str, chat_id: str = None, keyboard=None):
    """Send message via Bot API directly."""
    import aiohttp
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    async with aiohttp.ClientSession() as session:
        await session.post(url, json=payload)

def build_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📊 Status", "callback_data": "status"}, {"text": "📋 Shorts", "callback_data": "shorts"}],
            [{"text": "🧹 Cleanup", "callback_data": "cleanup"}, {"text": "📜 Logs", "callback_data": "logs"}],
        ]
    }

def status_text():
    shorts = get_shorts_status()
    total, used, free = get_disk_usage()
    posted = sum(1 for s in shorts if s["posted"])
    pending = len(shorts) - posted
    
    lines = [
        "📊 <b>omfplays Pipeline Status</b>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"🎬 Total shorts: <b>{len(shorts)}</b>",
        f"✅ Posted: <b>{posted}</b>",
        f"⏳ Pending: <b>{pending}</b>",
        "",
        f"💾 Disk: {format_bytes(used)} / {format_bytes(total)} ({format_bytes(free)} free)",
    ]
    return "\n".join(lines)

def shorts_text():
    shorts = get_shorts_status()
    if not shorts:
        return "No shorts found."
    
    lines = ["📋 <b>Shorts List</b>", ""]
    for s in shorts:
        icon = "✅" if s["posted"] else "⏳"
        size_mb = s["size"] / 1024 / 1024
        lines.append(f"{icon} <code>{s['name']}</code> — {size_mb:.1f} MB — {s['status']}")
    return "\n".join(lines)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 <b>omfplays Ops Bot</b>\n\n"
        "Commands:\n"
        "  /status — Pipeline overview\n"
        "  /shorts — List all shorts\n"
        "  /cleanup — Delete posted shorts\n"
        "  /logs — Recent workflow runs\n"
        "  /help — This message",
        parse_mode="HTML",
        reply_markup=build_keyboard()
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(status_text(), parse_mode="HTML", reply_markup=build_keyboard())

async def cmd_shorts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(shorts_text(), parse_mode="HTML", reply_markup=build_keyboard())

async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    msg = await update.message.reply_text("🧹 Running cleanup...", parse_mode="HTML")
    try:
        result = subprocess.run([sys.executable, "cleanup_shorts.py"], capture_output=True, text=True, timeout=60)
        output = result.stdout or result.stderr
        await msg.edit_text(f"🧹 <b>Cleanup Complete</b>\n<pre>{output[-3000:]}</pre>", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Cleanup failed: {e}", parse_mode="HTML")

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    try:
        result = subprocess.run(["gh", "run", "list", "--limit", "5", "--json", "conclusion,createdAt,headBranch,workflowName"], 
                                capture_output=True, text=True, timeout=30)
        runs = json.loads(result.stdout)
        lines = ["📜 <b>Recent Workflow Runs</b>", ""]
        for r in runs:
            icon = {"success": "✅", "failure": "❌", "cancelled": "⚠️"}.get(r.get("conclusion"), "ℹ️")
            dt = r.get("createdAt", "")[:16].replace("T", " ")
            lines.append(f"{icon} {r.get('workflowName')} — {dt} — {r.get('conclusion')}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch logs: {e}", parse_mode="HTML")

async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return
    
    data = query.data
    if data == "status":
        await query.edit_message_text(status_text(), parse_mode="HTML", reply_markup=build_keyboard())
    elif data == "shorts":
        await query.edit_message_text(shorts_text(), parse_mode="HTML", reply_markup=build_keyboard())
    elif data == "cleanup":
        await query.edit_message_text("🧹 Running cleanup...", parse_mode="HTML")
        try:
            result = subprocess.run([sys.executable, "cleanup_shorts.py"], capture_output=True, text=True, timeout=60)
            output = result.stdout or result.stderr
            await query.edit_text(f"🧹 <b>Cleanup Complete</b>\n<pre>{output[-3000:]}</pre>", parse_mode="HTML", reply_markup=build_keyboard())
        except Exception as e:
            await query.edit_text(f"❌ Cleanup failed: {e}", parse_mode="HTML", reply_markup=build_keyboard())
    elif data == "logs":
        await cmd_logs(query, context)

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")

def run_daemon():
    """Run bot as long-polling daemon."""
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("shorts", cmd_shorts))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CallbackQueryHandler(callback_query))
    app.add_error_handler(error_handler)
    
    print("🤖 Bot started. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

def run_one_shot(command: str):
    """Run single command for workflow (--status, --cleanup, etc)."""
    if command == "status":
        print(status_text())
    elif command == "shorts":
        print(shorts_text())
    elif command == "cleanup":
        subprocess.run([sys.executable, "cleanup_shorts.py"])
    elif command == "logs":
        try:
            result = subprocess.run(["gh", "run", "list", "--limit", "5"], capture_output=True, text=True)
            print(result.stdout)
        except:
            print("gh CLI not available")

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        asyncio.run(run_daemon())
    elif "--status" in sys.argv:
        run_one_shot("status")
    elif "--shorts" in sys.argv:
        run_one_shot("shorts")
    elif "--cleanup" in sys.argv:
        run_one_shot("cleanup")
    elif "--logs" in sys.argv:
        run_one_shot("logs")
    else:
        print("Usage:")
        print("  python telegram_bot.py --daemon      # Run as bot")
        print("  python telegram_bot.py --status      # One-shot status")
        print("  python telegram_bot.py --shorts      # List shorts")
        print("  python telegram_bot.py --cleanup     # Delete posted")
        print("  python telegram_bot.py --logs        # Recent runs")