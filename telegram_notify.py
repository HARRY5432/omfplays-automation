#!/usr/bin/env python3
"""
telegram_notify.py — PREMIUM Telegram notifications for omfplays pipeline.

Advanced Unicode box-drawing, emoji-rich formatting, progress bars,
pipeline stage indicators, and detailed stats. Used by both local pipeline
(day) and GitHub Actions (night).
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def _progress(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, int(round(done / total * width))))
    return "█" * filled + "▱" * (width - filled)


def _box(title: str, lines: list[str], icon: str, border: str = "═") -> str:
    """Render a premium Unicode box with title bar."""
    content_width = max(len(title) + 4, max((len(l) for l in lines), default=0) + 4)
    top = f"╭{border * (content_width - 2)}╮"
    title_line = f"│ {icon} {title}{' ' * (content_width - len(title) - len(icon) - 3)}│"
    mid = f"├{border * (content_width - 2)}┤"
    body = "\n".join(f"│ {l}{' ' * (content_width - len(l) - 3)}│" for l in lines)
    bottom = f"╰{border * (content_width - 2)}╯"
    return f"{top}\n{title_line}\n{mid}\n{body}\n{bottom}"


def _kv(label: str, value: str) -> str:
    return f"  {label}: <code>{value}</code>"


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def notify_pipeline_started(raw_status: dict, date_str: str, ist_now: str) -> bool:
    pct = (raw_status['consumed'] / raw_status['total'] * 100) if raw_status['total'] else 0
    lines = [
        _kv("📅 Date", date_str),
        _kv("🕐 Time", f"{ist_now} IST"),
        _kv("📥 Raw Total", f"{raw_status['total']} clip(s)"),
        _kv("✅ Consumed", f"{raw_status['consumed']} clip(s)"),
        _kv("⏳ Remaining", f"{raw_status['remaining']} clip(s)"),
        _kv("📊 Progress", f"{_progress(raw_status['consumed'], max(1, raw_status['total']))} {pct:.0f}%"),
        "",
        "🔄 <b>Pipeline:</b> Download → Build → Meta → Upload → Push",
        "☀️ <b>Mode:</b> Daytime Local Run",
    ]
    text = _box("🚀 LOCAL PIPELINE STARTED", lines, "🌅")
    return _send(f"<pre>{text}</pre>")


def notify_raw_empty() -> bool:
    lines = [
        "📂 <b>No raw clips left to process.</code>",
        "",
        "⬇️ <b>Downloading new videos from channels...</code>",
        "",
        "🔄 Pipeline will resume after download completes.",
    ]
    text = _box("⚠️ RAW EMPTY", lines, "📭")
    return _send(f"<pre>{text}</pre>")


def notify_raw_low(count: int) -> bool:
    lines = [
        f"📥 Only <code>{count}</code> raw clip(s) remaining.",
        "",
        "⬇️ <b>Downloading new videos to top up inventory...</code>",
        "",
        "🔄 Build will continue after download.",
    ]
    text = _box("⚠️ RAW LOW", lines, "📉")
    return _send(f"<pre>{text}</pre>")


def notify_short_built(short_name: str, index: int, total: int, title: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"🔢 Sequence: <code>{index} / {total}</code>",
        f"📝 Title: <code>{title}</code>",
        "",
        "✅ <b>Ready for upload.</b>",
    ]
    text = _box("🎞️ SHORT BUILT", lines, "🎬")
    return _send(f"<pre>{text}</pre>")


def notify_meta_generated(count: int, skipped: int) -> bool:
    lines = [
        f"✅ New meta: <code>{count}</code> short(s)",
        f"⏭️ Skipped: <code>{skipped}</code> short(s)",
        "",
        "🤖 <b>Powered by OpenRouter</code>",
        "🎯 Format: Day XX of Modern Warfare",
    ]
    text = _box("✨ META GENERATED", lines, "✍️")
    return _send(f"<pre>{text}</pre>")


def notify_upload_started(total_due: int, date_str: str, ist_now: str) -> bool:
    lines = [
        f"📅 Date: <code>{date_str}</code>",
        f"🕐 Time: <code>{ist_now}</code> IST",
        f"📤 Due for upload: <code>{total_due}</code> short(s)",
        "",
        "☀️ <b>Mode:</b> Daytime Local Upload",
        "⏰ <b>Slots:</b> 19:30 · 22:30 · 01:00 · 05:00 · 08:30 IST",
    ]
    text = _box("📤 UPLOAD STARTED", lines, "☀️")
    return _send(f"<pre>{text}</pre>")


def notify_short_uploaded(short_name: str, slot_time: str, slot_label: str, media_pk: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"🕐 Slot: <code>{slot_time}</code> IST",
        f"🏷️ Label: <code>{slot_label}</code>",
        f"🆔 Media PK: <code>{media_pk}</code>",
        "",
        "✅ <b>Published to Instagram.</b>",
    ]
    text = _box("✅ SHORT UPLOADED", lines, "📱")
    return _send(f"<pre>{text}</pre>")


def notify_upload_complete(posted: int, remaining: int, total: int) -> bool:
    pct = (posted / total * 100) if total else 0
    lines = [
        f"📤 Posted this run: <code>{posted}</code>",
        f"⏳ Remaining for night: <code>{remaining}</code>",
        f"📊 Total shorts: <code>{total}</code>",
        _kv("📊 Progress", f"{_progress(posted, total)} {pct:.0f}%"),
        "",
        "🌙 <b>Night upload will handle the rest.</b>",
    ]
    text = _box("📋 UPLOAD COMPLETE", lines, "📋")
    return _send(f"<pre>{text}</pre>")


def notify_raw_fully_processed(shorts_count: int, daily_limit: int) -> bool:
    lines = [
        f"🎬 Shorts built today: <code>{shorts_count}</code>",
        f"📊 Daily limit: <code>{daily_limit}</code>",
        f"📂 Raw clips: <code>All consumed</code>",
        "",
        "☀️ <b>Daytime upload done.</b>",
        "🌙 <b>Night upload will publish any remaining.</b>",
    ]
    text = _box("✅ RAW FULLY PROCESSED", lines, "🏁")
    return _send(f"<pre>{text}</pre>")


def notify_upload_failed(short_name: str, error: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"❌ Error: <code>{error}</code>",
        "",
        "🛑 <b>Stopping.</b> Progress saved in <code>state/uploads.json</code>.",
        "🔄 <b>Will retry on next scheduled run.</b>",
    ]
    text = _box("🚨 UPLOAD FAILED", lines, "💥")
    return _send(f"<pre>{text}</pre>")


def notify_download_failed() -> bool:
    lines = [
        "❌ <b>Download from channels failed.</b>",
        "🔍 <b>Check logs for details.</code>",
        "🔄 <b>Will retry on next scheduled run.</b>",
    ]
    text = _box("💥 DOWNLOAD FAILED", lines, "⛔")
    return _send(f"<pre>{text}</pre>")


def notify_build_failed() -> bool:
    lines = [
        "❌ <b>FFmpeg or build script error.</b>",
        "🔍 <b>Check logs for details.</code>",
        "🔄 <b>Will retry on next scheduled run.</b>",
    ]
    text = _box("💥 BUILD FAILED", lines, "⛔")
    return _send(f"<pre>{text}</pre>")


def notify_fatal(message: str) -> bool:
    lines = [
        f"❌ {message}",
        "",
        "🛑 <b>Pipeline stopped immediately.</b>",
    ]
    text = _box("💀 FATAL ERROR", lines, "💀")
    return _send(f"<pre>{text}</pre>")


def notify_night_upload_complete(posted: int, remaining: int, total: int) -> bool:
    pct = (posted / total * 100) if total else 0
    lines = [
        f"📤 Posted this run: <code>{posted}</code>",
        f"⏳ Remaining: <code>{remaining}</code>",
        f"📊 Total shorts: <code>{total}</code>",
        _kv("📊 Progress", f"{_progress(posted, total)} {pct:.0f}%"),
        "",
        "🌙 <b>Night upload complete.</b>",
    ]
    text = _box("🌙 NIGHT UPLOAD COMPLETE", lines, "🌙")
    return _send(f"<pre>{text}</pre>")


def notify_night_upload_failed(short_name: str, error: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"❌ Error: <code>{error}</code>",
        "",
        "🛑 <b>Stopping.</b> Progress saved.",
        "🔄 <b>Will retry on next night run.</b>",
    ]
    text = _box("🚨 NIGHT UPLOAD FAILED", lines, "💥")
    return _send(f"<pre>{text}</pre>")


def notify_meta_failed() -> bool:
    lines = [
        "❌ <b>OpenRouter meta generation failed.</b>",
        "🔍 <b>Shorts built but titles/descriptions may be missing.</b>",
        "🔄 <b>Will retry on next run.</b>",
    ]
    text = _box("⚠️ META FAILED", lines, "⚡")
    return _send(f"<pre>{text}</pre>")