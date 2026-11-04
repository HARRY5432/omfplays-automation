#!/usr/bin/env python3
"""
telegram_notify.py — Premium Telegram notifications for omfplays pipeline.

Advanced formatting with ASCII boxes, emojis, and detailed pipeline stats.
Used by both local pipeline (day) and GitHub Actions (night).
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


def _box(title: str, lines: list[str], icon: str = "📦") -> str:
    """Render a styled ASCII box."""
    width = max(len(title) + 4, max((len(l) for l in lines), default=0) + 4)
    top = f"{icon} ┌{'─' * (width - 2)}┐"
    mid_title = f"│ {title}{' ' * (width - len(title) - 3)}│"
    sep = f"├{'─' * (width - 2)}┤"
    body = "\n".join(f"│ {l}{' ' * (width - len(l) - 3)}│" for l in lines)
    bottom = f"└{'─' * (width - 2)}┘"
    return f"{top}\n{mid_title}\n{sep}\n{body}\n{bottom}"


def notify_pipeline_started(raw_status: dict, date_str: str, ist_now: str) -> bool:
    lines = [
        f"📅 Date: <code>{date_str}</code>",
        f"🕐 Time: <code>{ist_now}</code> IST",
        f"📥 Raw Total: <code>{raw_status['total']}</code>",
        f"✅ Consumed: <code>{raw_status['consumed']}</code>",
        f"⏳ Remaining: <code>{raw_status['remaining']}</code>",
        "",
        "🔄 Pipeline: Download → Build → Meta → Upload → Push",
    ]
    text = _box("🚀 LOCAL PIPELINE STARTED", lines, "🌅")
    return _send(f"<pre>{text}</pre>")


def notify_raw_empty() -> bool:
    lines = [
        "📂 No raw clips left to process.",
        "⬇️ Downloading new videos from channels...",
        "",
        "Pipeline will continue after download completes.",
    ]
    text = _box("⚠️ RAW EMPTY", lines, "📭")
    return _send(f"<pre>{text}</pre>")


def notify_raw_low(count: int) -> bool:
    lines = [
        f"Only <code>{count}</code> raw clip(s) left.",
        "⬇️ Downloading new videos...",
        "",
        "Topping up raw inventory before build.",
    ]
    text = _box("⚠️ RAW LOW", lines, "📉")
    return _send(f"<pre>{text}</pre>")


def notify_short_built(short_name: str, index: int, total: int, title: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"🔢 Sequence: <code>{index} / {total}</code>",
        f"📝 Title: <code>{title}</code>",
        "",
        "✅ Ready for upload.",
    ]
    text = _box("🎞️ SHORT BUILT", lines, "🎬")
    return _send(f"<pre>{text}</pre>")


def notify_meta_generated(count: int, skipped: int) -> bool:
    lines = [
        f"✅ New: <code>{count}</code>",
        f"⏭️ Skipped: <code>{skipped}</code>",
        "",
        "🤖 Powered by OpenRouter.",
    ]
    text = _box("✨ META GENERATED", lines, "✍️")
    return _send(f"<pre>{text}</pre>")


def notify_upload_started(total_due: int, date_str: str, ist_now: str) -> bool:
    lines = [
        f"📅 Date: <code>{date_str}</code>",
        f"🕐 Time: <code>{ist_now}</code> IST",
        f"📤 Due for upload: <code>{total_due}</code>",
        "",
        "☀️ Daytime upload slot.",
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
        "✅ Published to Instagram.",
    ]
    text = _box("✅ SHORT UPLOADED", lines, "📱")
    return _send(f"<pre>{text}</pre>")


def notify_upload_complete(posted: int, remaining: int, total: int) -> bool:
    lines = [
        f"📤 Posted this run: <code>{posted}</code>",
        f"⏳ Remaining for night: <code>{remaining}</code>",
        f"📊 Total shorts: <code>{total}</code>",
        "",
        "🌙 Night upload will handle the rest.",
    ]
    text = _box("📋 UPLOAD COMPLETE", lines, "📋")
    return _send(f"<pre>{text}</pre>")


def notify_raw_fully_processed(shorts_count: int, daily_limit: int) -> bool:
    lines = [
        f"🎬 Shorts built today: <code>{shorts_count}</code>",
        f"📊 Daily limit: <code>{daily_limit}</code>",
        f"📂 Raw clips: <code>All consumed</code>",
        "",
        "☀️ Daytime upload done. Night upload handles remaining.",
    ]
    text = _box("✅ RAW FULLY PROCESSED", lines, "🏁")
    return _send(f"<pre>{text}</pre>")


def notify_upload_failed(short_name: str, error: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"❌ Error: <code>{error}</code>",
        "",
        "🛑 Stopping. Progress saved in state/uploads.json.",
        "🔄 Will retry on next run.",
    ]
    text = _box("🚨 UPLOAD FAILED", lines, "💥")
    return _send(f"<pre>{text}</pre>")


def notify_download_failed() -> bool:
    lines = [
        "❌ Download from channels failed.",
        "🔍 Check logs for details.",
        "🔄 Will retry on next scheduled run.",
    ]
    text = _box("💥 DOWNLOAD FAILED", lines, "⛔")
    return _send(f"<pre>{text}</pre>")


def notify_build_failed() -> bool:
    lines = [
        "❌ FFmpeg or build script error.",
        "🔍 Check logs for details.",
        "🔄 Will retry on next scheduled run.",
    ]
    text = _box("💥 BUILD FAILED", lines, "⛔")
    return _send(f"<pre>{text}</pre>")


def notify_fatal(message: str) -> bool:
    lines = [
        f"❌ {message}",
        "",
        "🛑 Pipeline stopped immediately.",
    ]
    text = _box("💀 FATAL ERROR", lines, "💀")
    return _send(f"<pre>{text}</pre>")


def notify_night_upload_complete(posted: int, remaining: int, total: int) -> bool:
    lines = [
        f"📤 Posted this run: <code>{posted}</code>",
        f"⏳ Remaining: <code>{remaining}</code>",
        f"📊 Total shorts: <code>{total}</code>",
        "",
        "🌙 Night upload complete.",
    ]
    text = _box("🌙 NIGHT UPLOAD COMPLETE", lines, "🌙")
    return _send(f"<pre>{text}</pre>")


def notify_night_upload_failed(short_name: str, error: str) -> bool:
    lines = [
        f"🎬 Short: <code>{short_name}</code>",
        f"❌ Error: <code>{error}</code>",
        "",
        "🛑 Stopping. Progress saved.",
        "🔄 Will retry on next night run.",
    ]
    text = _box("🚨 NIGHT UPLOAD FAILED", lines, "💥")
    return _send(f"<pre>{text}</pre>")