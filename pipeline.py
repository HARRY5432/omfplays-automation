#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

sys.path.insert(0, str(HERE))
from telegram_notify import notify_master_report, notify_error

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def load_config():
    load_env()
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    if os.environ.get("OPENROUTER_API_KEY"):
        cfg["openrouter_api_key"] = os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("OPENROUTER_MODEL"):
        cfg["openrouter_model"] = os.environ["OPENROUTER_MODEL"]
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    return cfg


def get_batch_dir(cfg, date_str):
    return HERE / cfg["output_dir"] / f"batch_{date_str}"


def status_emoji(batch_dir):
    state = batch_dir / "status.json"
    if not state.exists():
        return "\u2714"
    with open(state, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("uploaded"):
        return "\u2714"
    return "\u23f3"


def mark_status(batch_dir, uploaded):
    state = batch_dir / "status.json"
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"uploaded": uploaded, "ts": datetime.now().isoformat()}, f)


def run_step(label, cmd, cwd=None):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=cwd or str(HERE))
    if result.returncode != 0:
        print(f"FAILED: {label} (exit {result.returncode})")
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def generate_viral_meta(video_path, batch_dir, idx):
    from generate_captions import generate_package, write_meta_file
    topics = ["ASMR triggers", "satisfying slime", "funny cat fails",
              "kinetic sand cutting", "soap carving", "hydraulic press",
              "cat vs cucumber", "paint mixing", "power washing",
              "cat jumping fails"]
    styles = ["asmr", "satisfying", "funny_cat"]
    topic = topics[(idx - 1) % len(topics)]
    style = styles[(idx - 1) % len(styles)]
    pkg = generate_package(topic, style)
    txt_path = batch_dir / f"{video_path.stem}.txt"
    write_meta_file(pkg, txt_path)


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    cfg = load_config()
    daily = int(cfg.get("daily_shorts", 5))
    today = datetime.now().date()

    date_str = today.strftime("%Y-%m-%d")
    batch_dir = get_batch_dir(cfg, date_str)
    emoji = status_emoji(batch_dir)
    print(f"\n\U0001f4c5 {date_str} {emoji}  (fresh batch, target: {daily} shorts)")

    if batch_dir.exists() and (batch_dir / "status.json").exists():
        with open(batch_dir / "status.json", "r", encoding="utf-8") as f:
            st = json.load(f)
        if st.get("uploaded") and not force:
            print("Batch already uploaded today. Use --force to redo.")
            sys.exit(0)

    batch_dir.mkdir(parents=True, exist_ok=True)

    shorts_dir = HERE / cfg["output_dir"]
    manifest_path = shorts_dir / "manifest.json"

    offset = 0
    needed_total = daily
    existing_shorts = sorted(shorts_dir.glob("s*.mp4"))
    available_before = len(existing_shorts)

    downloaded = 0
    built = 0
    uploaded_count = 0
    errors = []

    try:
        if available_before >= needed_total and not force:
            print(f"Enough shorts already exist ({available_before} >= {needed_total}). Skipping download + build.")
        else:
            run_step("STEP 1: Download from channels",
                     [sys.executable, str(HERE / "download_channels.py")])
            after_download = len(list(shorts_dir.glob("s*.mp4")))
            downloaded = max(0, after_download - available_before)

            build_args = [sys.executable, str(HERE / "build_shorts.py")]
            if force:
                build_args.append("--force")
            build_args += ["--max-shorts", str(max(1, needed_total - available_before))]
            run_step("STEP 2: Build shorts (40s clips)", build_args)
            after_build = len(list(shorts_dir.glob("s*.mp4")))
            built = max(0, after_build - after_download)

        all_shorts = sorted(shorts_dir.glob("s*.mp4"))
        if len(all_shorts) < daily:
            raise RuntimeError(f"Not enough shorts ({len(all_shorts)}) for today's batch of {daily}.")

        batch_shorts = all_shorts[offset:offset + daily]
        if len(batch_shorts) < daily:
            raise RuntimeError(f"Not enough Shorts for today's fresh batch. Need {daily}, have {len(batch_shorts)}.")

        for i, src in enumerate(batch_shorts, 1):
            dest = batch_dir / src.name
            if not dest.exists() or force:
                shutil.copy2(str(src), str(dest))
            generate_viral_meta(dest, batch_dir, i)

        print(f"\n\u2705 Batch ready: {len(batch_shorts)} shorts in {batch_dir}")
        print(f"   Status: \u23f3 pending upload")

        if not dry_run:
            run_step("STEP 3: Upload to YouTube",
                     [sys.executable, str(HERE / "yt_upload.py"),
                      str(batch_dir), "--privacy", "public"])
            mark_status(batch_dir, True)
            uploaded_count = len(batch_shorts)
            print(f"\n\u2705 {date_str} \u2714  Uploaded!")
        else:
            print("\n(dry run - skipping upload)")

    except RuntimeError as e:
        errors.append(str(e))
        notify_error("Pipeline", str(e))

    notify_master_report(date_str, daily, downloaded, built, uploaded_count, batch_dir.name, errors if errors else None)

    next_date = today + timedelta(days=1)
    next_batch = get_batch_dir(cfg, next_date.strftime("%Y-%m-%d"))
    print(f"\n\u27a1\ufe0f  Next batch: {next_date.strftime('%Y-%m-%d')} \u23f3")


if __name__ == "__main__":
    main()

