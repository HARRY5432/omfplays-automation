#!/usr/bin/env python3
"""
telegram_bot.py — Interactive Telegram ops bot for omfplays.

Commands:
  /status   — Pipeline overview
  /shorts   — List all shorts with status
  /raw      — Raw clips status (pending vs consumed)
  /cleanup  — Delete posted shorts from disk
  /logs     — Recent workflow runs
  /help     — Show commands

Modes:
  --daemon   Long-polling bot (interactive)
  --status   One-shot status (for cron/workflow)
  --shorts   One-shot list
  --raw      One-shot raw status
  --cleanup  One-shot cleanup
  --logs     One-shot recent runs
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
    from telegram import Update
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
except ImportError:
    import subprocess as _sp
    _sp.run([sys.executable, "-m", "pip", "install", "python-telegram-bot[job-queue]", "--break-system-packages"], check=True)
    from telegram import Update
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"
MANIFEST_PATH = SHORTS_DIR / "manifest.json"

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


def format_bytes(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def get_shorts_status() -> list[dict]:
    uploads = load_json(UPLOADS_PATH, {})
    posted = {v.get("short") for v in uploads.values() if v.get("status") == "posted"}
    shorts = []
    if SHORTS_DIR.exists():
        for f in sorted(SHORTS_DIR.glob("s*.mp4")):
            stem = f.stem
            size = f.stat().st_size
            shorts.append({
                "name": stem, "size": size,
                "status": "✅ Posted" if stem in posted else "⏳ Pending",
                "posted": stem in posted,
            })
    return shorts


def get_disk_usage() -> tuple[int, int, int]:
    try:
        import shutil
        return shutil.disk_usage(HERE)
    except Exception:
        return 0, 0, 0


def build_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📊 Status", "callback_data": "status"},
             {"text": "📋 Shorts", "callback_data": "shorts"}],
            [{"text": "📂 Raw", "callback_data": "raw"},
             {"text": "🧹 Cleanup", "callback_data": "cleanup"}],
            [{"text": "📜 Logs", "callback_data": "logs"}],
        ]
    }


def status_text() -> str:
    """Shared command-centre block (same view the pipeline sends)."""
    try:
        import html

        import pipeline_status as ps

        return f"<pre>{html.escape(ps.render(ps.snapshot()))}</pre>"
    except Exception:
        shorts = get_shorts_status()
        total, used, free = get_disk_usage()
        posted = sum(1 for s in shorts if s["posted"])
        pending = len(shorts) - posted
        return (
            "📊 <b>omfplays Pipeline Status</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            "\n"
            f"🎬 Total shorts: <b>{len(shorts)}</b>\n"
            f"✅ Posted: <b>{posted}</b>\n"
            f"⏳ Pending: <b>{pending}</b>\n"
            "\n"
            f"💾 Disk: {format_bytes(used)} / {format_bytes(total)} ({format_bytes(free)} free)"
        )


def shorts_text() -> str:
    shorts = get_shorts_status()
    if not shorts:
        return "No shorts found."
    lines = ["📋 <b>Shorts List</b>", ""]
    for s in shorts:
        icon = "✅" if s["posted"] else "⏳"
        size_mb = s["size"] / 1024 / 1024
        lines.append(f"{icon} <code>{s['name']}</code> — {size_mb:.1f} MB — {s['status']}")
    return "\n".join(lines)


def raw_status_text() -> str:
    if not SHORTS_DIR.exists():
        return "📂 <b>Raw Clips Status</b>\n\nNo shorts/ folder found."
    raw_dir = HERE / "raw"
    if not raw_dir.exists():
        return "📂 <b>Raw Clips Status</b>\n\nNo raw/ folder found."
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}
    raw_files = [f for f in raw_dir.iterdir() if f.is_file() and f.suffix.lower() in video_exts]
    manifest = load_json(MANIFEST_PATH, [])
    consumed = {e.get("source") for e in manifest if isinstance(e, dict)}
    remaining = [f for f in raw_files if f.name not in consumed]
    done = len(raw_files) - len(remaining)
    lines = [
        "📂 <b>Raw Clips Status</b>",
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        "",
        f"📥 Total: <b>{len(raw_files)}</b>",
        f"✅ Consumed: <b>{done}</b>",
        f"⏳ Remaining: <b>{len(remaining)}</b>",
        "",
    ]
    if remaining:
        lines.append("<b>Pending:</b>")
        for f in remaining[:10]:
            size_mb = f.stat().st_size / 1024 / 1024
            lines.append(f"  ⏳ <code>{f.name}</code> ({size_mb:.1f} MB)")
        if len(remaining) > 10:
            lines.append(f"  … and {len(remaining) - 10} more")
        lines.append("")
        lines.append("Run local pipeline to process remaining.")
    else:
        lines.append("✅ All clips consumed.")
    return "\n".join(lines)


async def send_tg_message(text: str, chat_id: str | None = None, keyboard: dict | None = None):
    import aiohttp
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    async with aiohttp.ClientSession() as session:
        await session.post(url, json=payload)


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 <b>omfplays Ops Bot</b>\n\n"
        "Commands:\n"
        "  /status  — Pipeline overview\n"
        "  /shorts  — List all shorts\n"
        "  /raw     — Raw clips status\n"
        "  /cleanup — Delete posted shorts\n"
        "  /logs    — Recent workflow runs\n"
        "  /help    — This message",
        parse_mode="HTML",
        reply_markup=build_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(status_text(), parse_mode="HTML", reply_markup=build_keyboard())


async def cmd_shorts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(shorts_text(), parse_mode="HTML", reply_markup=build_keyboard())


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(raw_status_text(), parse_mode="HTML", reply_markup=build_keyboard())


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    msg = await update.message.reply_text("🧹 Running cleanup...", parse_mode="HTML")
    try:
        result = subprocess.run([sys.executable, "cleanup_shorts.py"], capture_output=True,
                                text=True, timeout=60)
        output = (result.stdout or result.stderr)[-3000:]
        await msg.edit_text(f"🧹 <b>Cleanup Complete</b>\n<pre>{output}</pre>",
                            parse_mode="HTML", reply_markup=build_keyboard())
    except Exception as e:
        await msg.edit_text(f"❌ Cleanup failed: {e}", parse_mode="HTML", reply_markup=build_keyboard())


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    try:
        result = subprocess.run(["gh", "run", "list", "--limit", "5"],
                                capture_output=True, text=True, timeout=30)
        lines = ["📜 <b>Recent Workflow Runs</b>", ""]
        for line in result.stdout.strip().splitlines()[:5]:
            lines.append(f"  {line}")
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
    elif data == "raw":
        await query.edit_message_text(raw_status_text(), parse_mode="HTML", reply_markup=build_keyboard())
    elif data == "cleanup":
        await query.edit_message_text("🧹 Running cleanup...", parse_mode="HTML")
        try:
            result = subprocess.run([sys.executable, "cleanup_shorts.py"], capture_output=True,
                                    text=True, timeout=60)
            output = (result.stdout or result.stderr)[-3000:]
            await query.edit_text(f"🧹 <b>Cleanup Complete</b>\n<pre>{output}</pre>",
                                  parse_mode="HTML", reply_markup=build_keyboard())
        except Exception as e:
            await query.edit_text(f"❌ Cleanup failed: {e}", parse_mode="HTML", reply_markup=build_keyboard())
    elif data == "logs":
        await cmd_logs(query, context)


def run_daemon():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", flush=True)
        sys.exit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("shorts", cmd_shorts))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CallbackQueryHandler(callback_query))
    print("🤖 Bot started (daemon mode). Press Ctrl+C to stop.", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_one_shot(command: str):
    if command == "status":
        print(status_text())
    elif command == "shorts":
        print(shorts_text())
    elif command == "raw":
        print(raw_status_text())
    elif command == "cleanup":
        subprocess.run([sys.executable, "cleanup_shorts.py"])
    elif command == "logs":
        try:
            result = subprocess.run(["gh", "run", "list", "--limit", "5"], capture_output=True, text=True)
            print(result.stdout)
        except Exception:
            print("gh CLI not available")


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        # run_polling() manages its own event loop; wrapping it in
        # asyncio.run() made the daemon fail on startup.
        run_daemon()
    elif "--status" in sys.argv:
        run_one_shot("status")
    elif "--shorts" in sys.argv:
        run_one_shot("shorts")
    elif "--raw" in sys.argv:
        run_one_shot("raw")
    elif "--cleanup" in sys.argv:
        run_one_shot("cleanup")
    elif "--logs" in sys.argv:
        run_one_shot("logs")
    else:
        print("Usage:")
        print("  python telegram_bot.py --daemon   # Interactive bot")
        print("  python telegram_bot.py --status   # One-shot status")
        print("  python telegram_bot.py --shorts   # List shorts")
        print("  python telegram_bot.py --raw      # Raw status")
        print("  python telegram_bot.py --cleanup  # Delete posted")
        print("  python telegram_bot.py --logs     # Recent runs")