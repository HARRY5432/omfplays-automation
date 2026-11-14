#!/usr/bin/env python3
"""
telegram_bot.py — Interactive Telegram control panel for omfplays.

Tier 2: Inline Keyboard System
- Edit-in-place messages (no spam)
- Paginated lists (shorts, logs, raw)
- Confirmation dialogs (cleanup, shutdown)
- PC heartbeat gating (buttons only work when PC online)
- Progress bar rendering for build actions

Usage:
  python telegram_bot.py --daemon   # Interactive bot
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        CallbackQuery,
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        filters,
        ContextTypes,
    )
except ImportError:
    import subprocess as _sp
    _sp.run([sys.executable, "-m", "pip", "install", "python-telegram-bot[job-queue]", "--break-system-packages"], check=True)
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        CallbackQuery,
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        filters,
        ContextTypes,
    )

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
LOGS_DIR = HERE / "logs"
PIPELINE_LOG = LOGS_DIR / "pipeline.log"
UPLOADS_PATH = STATE_DIR / "uploads.json"
SHORTS_DIR = HERE / "shorts"
RAW_DIR = HERE / "raw"
HEARTBEAT_PATH = STATE_DIR / "bot_heartbeat.json"
SESSION_PATH = STATE_DIR / "bot_session.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = set(os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")) if os.getenv("TELEGRAM_ALLOWED_USERS") else set()

# Items per page for pagination
PAGE_SIZE = 10

# Build progress tracking (in-memory during a run)
BUILD_PROGRESS = {
    "running": False,
    "current": 0,
    "total": 0,
    "current_file": "",
    "failed": 0,
}


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS


# ══════════════════════════════════════════════════════════════════════════════════
# HEARTBEAT / PC ONLINE STATE
# ═════════════════════════════════════════════════════════════════════════════════

def get_heartbeat() -> dict:
    return load_json(HEARTBEAT_PATH, {"pc_online": False, "last_heartbeat": None})


def is_pc_online() -> bool:
    hb = get_heartbeat()
    if not hb.get("pc_online"):
        return False
    last = hb.get("last_heartbeat")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        now = datetime.now(timezone.utc)
        if (now - last_dt).total_seconds() > 1800:  # 30 min
            return False
    except Exception:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════════
# SESSION STATE (for confirmation dialogs)
# ═════════════════════════════════════════════════════════════════════════════════

def get_session() -> dict:
    return load_json(SESSION_PATH, {})


def save_session(data: dict) -> None:
    save_json(SESSION_PATH, data)


def clear_session(user_id: int) -> None:
    sess = get_session()
    sess.pop(str(user_id), None)
    save_session(sess)


def set_pending_action(user_id: int, action: str) -> None:
    sess = get_session()
    sess[str(user_id)] = {
        "awaiting_confirmation": action,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_session(sess)


def get_pending_action(user_id: int) -> str | None:
    sess = get_session()
    entry = sess.get(str(user_id))
    if not entry:
        return None
    # Expire after 5 minutes
    try:
        ts = datetime.fromisoformat(entry.get("timestamp", ""))
        if (datetime.now(timezone.utc) - ts).total_seconds() > 300:
            clear_session(user_id)
            return None
    except Exception:
        pass
    return entry.get("awaiting_confirmation")


# ══════════════════════════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ═════════════════════════════════════════════════════════════════════════════════

def keyboard_main(pc_online: bool) -> InlineKeyboardMarkup:
    if pc_online:
        buttons = [
            [
                InlineKeyboardButton("📊 Status", callback_data="nav:status"),
                InlineKeyboardButton("📜 Logs", callback_data="nav:logs:1"),
            ],
            [
                InlineKeyboardButton("🎬 Shorts", callback_data="nav:shorts:1"),
                InlineKeyboardButton("📂 Raw", callback_data="nav:raw"),
            ],
            [
                InlineKeyboardButton("▶️ Build", callback_data="action:build"),
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh:main"),
            ],
            [
                InlineKeyboardButton("⏹️ Shutdown PC", callback_data="action:shutdown"),
                InlineKeyboardButton("❓ Help", callback_data="nav:help"),
            ],
        ]
    else:
        buttons = [
            [
                InlineKeyboardButton("🔴 PC Offline", callback_data="nav:offline"),
            ],
            [
                InlineKeyboardButton("📊 Status (Read-Only)", callback_data="nav:status"),
                InlineKeyboardButton("📜 Logs (Read-Only)", callback_data="nav:logs:1"),
            ],
            [
                InlineKeyboardButton("🎬 Shorts (Read-Only)", callback_data="nav:shorts:1"),
                InlineKeyboardButton("📂 Raw (Read-Only)", callback_data="nav:raw"),
            ],
        ]
    return InlineKeyboardMarkup(buttons)


def keyboard_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Menu", callback_data="nav:main")]])


def keyboard_back_refresh(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Back", callback_data="nav:main"),
         InlineKeyboardButton("🔄 Refresh", callback_data=callback_data)],
    ])


def keyboard_short_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"nav:shorts:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="nav:shorts:current"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"nav:shorts:{page + 1}"))
    nav_buttons.append(InlineKeyboardButton("◀ Back to Menu", callback_data="nav:main"))
    return InlineKeyboardMarkup([nav_buttons])


def keyboard_confirm(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm:{action}"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:main"),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════════
# PAGE RENDERERS
# ═════════════════════════════════════════════════════════════════════════════════

def render_main(pc_online: bool) -> tuple[str, InlineKeyboardMarkup]:
    hb = get_heartbeat()
    status_icon = "🟢" if pc_online else "🔴"
    status_text = "Online" if pc_online else "Offline"
    last_hb = hb.get("last_heartbeat", "never")
    text = (
        f"{status_icon} omfplays Control Panel\n"
        f"PC: {status_text} · Last heartbeat: {last_hb}\n\n"
        f"Select a module below:"
    )
    return text, keyboard_main(pc_online)


def render_status() -> tuple[str, InlineKeyboardMarkup]:
    try:
        import pipeline_status as ps
        snap = ps.snapshot()
        lines = [
            "📊 Pipeline Status",
            "",
            f"🕐 {snap['now_ist']}",
            f"🖥  node: {snap['node'].upper()}",
            f"📅 slots: {snap['shorts']['posted_today']}/{snap['shorts']['slots_total']} posted today",
            f"🎬 shorts waiting: {snap['shorts']['pending']}",
            f"📦 manifest: {snap['manifest']['total']} entries",
            f"💾 disk: {snap['disk_free_gb']} GB free",
            "",
            f"📥 raw videos: {snap['raw']['total']}",
            f"🎞 reels left: {snap['raw'].get('slices_left', 0)} × {snap['raw'].get('short_secs', 40)}s",
        ]
        if snap["attention"]:
            lines.append("")
            lines.append("⚠ Attention:")
            for item in snap["attention"][:5]:
                lines.append(f"   • {item}")
        text = "\n".join(lines)
    except Exception as e:
        text = f"Status error: {e}"
    return text, keyboard_back_refresh("refresh:status")


def render_shorts_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    uploads = load_json(UPLOADS_PATH, {})
    posted = {v.get("short") for v in uploads.values() if v.get("status") == "posted"}
    if not SHORTS_DIR.exists():
        text = "🎬 Shorts\n\nNo shorts folder found."
        return text, keyboard_back()

    all_shorts = sorted(SHORTS_DIR.glob("s*.mp4"))
    total = len(all_shorts)
    if total == 0:
        text = "🎬 Shorts\n\nNo shorts found."
        return text, keyboard_back()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_shorts = all_shorts[start:end]

    lines = [f"🎬 Shorts (Page {page} of {total_pages})", "● Posted   ○ Pending", ""]
    for f in page_shorts:
        icon = "✅" if f.stem in posted else "○"
        size_mb = f.stat().st_size / 1024 / 1024
        lines.append(f"{icon} {f.stem}  {size_mb:.1f} MB")

    text = "\n".join(lines)
    return text, keyboard_short_pagination(page, total_pages)


def render_raw() -> tuple[str, InlineKeyboardMarkup]:
    if not RAW_DIR.exists():
        text = "📂 Raw Library\n\nNo raw folder found."
        return text, keyboard_back()

    vids = [f for f in RAW_DIR.iterdir() if f.is_file() and f.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}]
    if not vids:
        text = "📂 Raw Library\n\nRaw folder is empty."
        return text, keyboard_back()

    lines = [f"📂 Raw Library", f"📥 {len(vids)} raw video(s)", ""]
    for v in vids[:15]:
        size_mb = v.stat().st_size / 1024 / 1024
        lines.append(f"• {v.name}  ({size_mb:.1f} MB)")
    if len(vids) > 15:
        lines.append(f"   … and {len(vids) - 15} more")

    text = "\n".join(lines)
    return text, keyboard_back_refresh("refresh:raw")


def render_logs_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    if not PIPELINE_LOG.exists():
        text = "📜 Pipeline Logs\n\nNo pipeline.log found yet."
        return text, keyboard_back()

    lines = PIPELINE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        text = "📜 Pipeline Logs\n\nNo log entries."
        return text, keyboard_back()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_lines = lines[start:end]

    text_lines = [f"📜 Pipeline Logs (Page {page} of {total_pages})", ""]
    text_lines.extend(page_lines[-PAGE_SIZE:])

    text = "\n".join(text_lines)
    return text, keyboard_short_pagination(page, total_pages)


def render_slots() -> tuple[str, InlineKeyboardMarkup]:
    try:
        import pipeline_status as ps
        snap = ps.snapshot()
        rows = snap.get("slots_today", [])
        lines = ["📅 Today's Slots", ""]
        for row in rows:
            glyph = {"posted": "✅", "due": "⏳", "missed": "✖", "scheduled": "·"}.get(row["state"], "·")
            time_str = row.get("time", "?")
            state = row.get("state", "unknown")
            short = row.get("short", "")
            detail = f" · {short}" if short else ""
            lines.append(f"{glyph} {time_str} UTC — {state}{detail}")
        text = "\n".join(lines)
    except Exception as e:
        text = f"Slots error: {e}"
    return text, keyboard_back()


def render_help() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "❓ omfplays Help\n\n"
        "Available modules:\n"
        "• 📊 Status — System overview, disk, raw library\n"
        "• 📜 Logs — Pipeline execution logs (paginated)\n"
        "• 🎬 Shorts — All built shorts with status (paginated)\n"
        "• 📂 Raw — Raw footage inventory\n"
        "• 📅 Slots — Instagram upload schedule\n"
        "• ▶️ Build — Start shorts build pipeline\n"
        "• ⏹️ Shutdown PC — Power off this machine\n"
        "\n"
        "PC heartbeat: Bot checks if PC is online before allowing actions.\n"
        "If PC is offline, only read-only pages are available."
    )
    return text, keyboard_back()


def render_offline() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔴 PC Offline\n\n"
        "The Telegram bot daemon is not running on this machine.\n\n"
        "Read-only pages are still available:\n"
        "• 📊 Status — Last known system state\n"
        "• 📜 Logs — Last known logs\n"
        "• 🎬 Shorts — Last known shorts inventory\n"
        "• 📂 Raw — Last known raw library\n"
        "\n"
        "To enable interactive actions (Build, Shutdown, Cleanup),\n"
        "start the bot daemon on this PC:\n"
        "  python telegram_bot.py --daemon"
    )
    return text, keyboard_back()


# ══════════════════════════════════════════════════════════════════════════════════
# PROGRESS BAR
# ═════════════════════════════════════════════════════════════════════════════════

def render_build_progress() -> tuple[str, InlineKeyboardMarkup]:
    p = BUILD_PROGRESS
    if not p["running"] and p["current"] == 0 and p["total"] == 0:
        text = "🏗️ Build Pipeline\n\nNo build in progress."
        return text, keyboard_back()

    percent = int((p["current"] / p["total"]) * 100) if p["total"] > 0 else 0
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    lines = [
        "🏗️ Building Shorts",
        "",
        f"[{bar}] {percent}%",
        "",
        f"Current: {p['current_file']}",
        f"Source: {p.get('current_source', '—')}",
        "",
        f"Completed: {p['current']} of {p['total']} shorts",
        f"Failed: {p['failed']}",
    ]
    if p["running"]:
        lines.append("")
        lines.append("[⏹️ Cancel Build]")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Cancel Build", callback_data="action:cancel_build")],
                                   [InlineKeyboardButton("◀ Back to Menu", callback_data="nav:main")]])
    else:
        lines.append("")
        lines.append("Build complete.")
        kb = keyboard_back()
    return "\n".join(lines), kb


# ══════════════════════════════════════════════════════════════════════════════════
# ACTION EXECUTORS
# ═════════════════════════════════════════════════════════════════════════════════

async def run_build_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger build_shorts_omf.py and show progress."""
    pc_online = is_pc_online()
    if not pc_online:
        await update.callback_query.edit_message_text(
            "🔴 PC Offline — Cannot start build. Start the bot daemon first.",
            reply_markup=keyboard_back(),
        )
        return

    query = update.callback_query
    BUILD_PROGRESS["running"] = True
    BUILD_PROGRESS["current"] = 0
    BUILD_PROGRESS["total"] = 0
    BUILD_PROGRESS["current_file"] = ""
    BUILD_PROGRESS["failed"] = 0

    # Initial progress message
    text, kb = render_build_progress()
    await query.edit_message_text(text, reply_markup=kb)

    import subprocess
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "build_shorts_omf.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        BUILD_PROGRESS["running"] = False
        if proc.returncode == 0:
            text = f"✅ Build Complete\n\n{stdout.decode('utf-8', errors='replace')[-800:]}"
        else:
            text = f"❌ Build Failed (exit {proc.returncode})\n\n{stderr.decode('utf-8', errors='replace')[-800:]}"
    except asyncio.TimeoutError:
        BUILD_PROGRESS["running"] = False
        text = "⏱️ Build timed out after 10 minutes."
    except Exception as e:
        BUILD_PROGRESS["running"] = False
        text = f"💥 Build error: {e}"

    await query.edit_message_text(text, reply_markup=keyboard_back())


async def run_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm and execute PC shutdown."""
    query = update.callback_query
    await query.edit_message_text(
        "⏹️ Confirm PC Shutdown\n\n"
        "The PC will power off after the current pipeline finishes.\n\n"
        "You will need to manually turn it back on to resume the bot.",
        reply_markup=keyboard_confirm("shutdown"),
    )


async def execute_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Actually shut down the PC."""
    query = update.callback_query
    await query.edit_message_text("💤 Shutting down PC…")
    import subprocess
    try:
        subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
    except Exception as e:
        await query.edit_message_text(f"❌ Shutdown failed: {e}", reply_markup=keyboard_back())


async def run_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm and execute cleanup of posted shorts."""
    query = update.callback_query
    await query.edit_message_text(
        "⚠ Confirm Cleanup\n\n"
        "This will delete all posted shorts from disk.\n"
        "This action cannot be undone.",
        reply_markup=keyboard_confirm("cleanup"),
    )


async def execute_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Actually run cleanup_shorts.py."""
    query = update.callback_query
    await query.edit_message_text("🗑️ Running cleanup…")
    import subprocess
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "cleanup_shorts.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            text = f"✅ Cleanup Complete\n\n{stdout.decode('utf-8', errors='replace')[-600:]}"
        else:
            text = f"❌ Cleanup Failed\n\n{stderr.decode('utf-8', errors='replace')[-600:]}"
    except asyncio.TimeoutError:
        text = "⏱️ Cleanup timed out."
    except Exception as e:
        text = f"💥 Cleanup error: {e}"
    await query.edit_message_text(text, reply_markup=keyboard_back())


# ══════════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ═════════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    pc_online = is_pc_online()
    text, kb = render_main(pc_online)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text, kb = render_status()
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text, kb = render_logs_page(1)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_shorts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text, kb = render_shorts_page(1)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text, kb = render_raw()
    await update.message.reply_text(text, reply_markup=kb)


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        await query.edit_message_text("⛔ Not authorized.")
        return

    data = query.data or ""

    # Handle pending confirmation actions first
    pending = get_pending_action(query.from_user.id)
    if pending and data not in (f"confirm:{pending}", "nav:main"):
        # User tapped something else while awaiting confirmation — clear it
        clear_session(query.from_user.id)

    # Route by callback data
    if data == "nav:main":
        pc_online = is_pc_online()
        text, kb = render_main(pc_online)
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "nav:status":
        text, kb = render_status()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "refresh:status":
        text, kb = render_status()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "refresh:main":
        pc_online = is_pc_online()
        text, kb = render_main(pc_online)
        await query.edit_message_text(text, reply_markup=kb)

    elif data.startswith("nav:shorts:"):
        parts = data.split(":")
        if parts[2] == "current":
            # Disabled page indicator — no-op
            return
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = render_shorts_page(page)
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "nav:raw":
        text, kb = render_raw()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "refresh:raw":
        text, kb = render_raw()
        await query.edit_message_text(text, reply_markup=kb)

    elif data.startswith("nav:logs:"):
        parts = data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = render_logs_page(page)
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "nav:slots":
        text, kb = render_slots()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "nav:help":
        text, kb = render_help()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "nav:offline":
        text, kb = render_offline()
        await query.edit_message_text(text, reply_markup=kb)

    elif data == "action:build":
        await run_build_pipeline(update, context)

    elif data == "action:shutdown":
        pc_online = is_pc_online()
        if not pc_online:
            await query.edit_message_text(
                "🔴 PC Offline — Cannot shutdown.",
                reply_markup=keyboard_back(),
            )
            return
        await run_shutdown(update, context)

    elif data == "confirm:shutdown":
        pending = get_pending_action(query.from_user.id)
        if pending != "shutdown":
            await query.edit_message_text("⚠️ Confirmation expired. Try again.", reply_markup=keyboard_back())
            return
        clear_session(query.from_user.id)
        await execute_shutdown(update, context)

    elif data == "action:cleanup":
        pc_online = is_pc_online()
        if not pc_online:
            await query.edit_message_text(
                "🔴 PC Offline — Cannot cleanup.",
                reply_markup=keyboard_back(),
            )
            return
        await run_cleanup(update, context)

    elif data == "confirm:cleanup":
        pending = get_pending_action(query.from_user.id)
        if pending != "cleanup":
            await query.edit_message_text("⚠️ Confirmation expired. Try again.", reply_markup=keyboard_back())
            return
        clear_session(query.from_user.id)
        await execute_cleanup(update, context)

    elif data == "action:cancel_build":
        BUILD_PROGRESS["running"] = False
        await query.edit_message_text("⏹️ Build cancelled by user.", reply_markup=keyboard_back())

    else:
        await query.edit_message_text(f"❓ Unknown action: {data}", reply_markup=keyboard_back())


# ══════════════════════════════════════════════════════════════════════════════════
# DAEMON
# ═════════════════════════════════════════════════════════════════════════════════

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
    app.add_handler(CallbackQueryHandler(callback_query))
    print("Bot started. Press Ctrl+C to stop.", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        print("Usage: python telegram_bot.py --daemon")
