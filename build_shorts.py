#!/usr/bin/env python3
"""
build_shorts.py - raw footage -> sequential shorts, no intermediate steps.

Every new file dropped in raw/ is sliced into short_seconds-long blocks
(default 45s): shorts/s001.mp4 = 0-45s, s002.mp4 = 45-90s, and so on.
The final block keeps whatever time is left, even if shorter.

Run:
  python build_shorts.py             # process whatever is new, then exit
  python build_shorts.py --watch     # keep running; pick up new files as they land
  python build_shorts.py --dry-run   # show what would happen, change nothing
  python build_shorts.py --force     # rebuild everything from scratch

Requires ffmpeg + ffprobe on PATH.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}

WATCH_POLL_SECONDS = 10
STABILITY_CHECK_SECONDS = 2

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def die(message):
    print(f"\nERROR: {message}")
    sys.exit(1)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        die(f"config.json not found next to this script ({CONFIG_PATH})")
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_videos(directory, strict=True):
    if not os.path.isdir(directory):
        if strict:
            die(f"folder '{directory}' not found. Create it and drop your footage in it.")
        return []
    files = [
        os.path.join(directory, n)
        for n in os.listdir(directory)
        if os.path.splitext(n)[1].lower() in VIDEO_EXTENSIONS
    ]
    return sorted(files, key=lambda p: natural_key(os.path.basename(p)))


def manifest_path(output_dir):
    return os.path.join(output_dir, "manifest.json")


def load_manifest(output_dir):
    p = manifest_path(output_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_manifest(output_dir, manifest):
    with open(manifest_path(output_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def recover_manifest(output_dir, raw_dir, ffprobe, short_seconds):
    """Recover metadata after an interrupted build that created the MP4s first.

    Older runs could finish all encodes and be interrupted before manifest.json
    was written. Reusing those clips is both faster and safer than encoding the
    same source again.
    """
    existing = {n.lower() for n in os.listdir(output_dir)}
    recovered = []
    next_index = 1
    for src in list_videos(raw_dir, strict=False):
        dur = probe_duration(ffprobe, src) if ffprobe else None
        if dur is None:
            continue
        t = 0.0
        entries = []
        while t < dur:
            name = f"s{next_index:03d}.mp4"
            w = min(short_seconds, dur - t)
            entries.append({"short": name, "source": os.path.basename(src),
                            "start": round(t, 3), "duration": round(w, 3)})
            next_index += 1
            t += w
        if all(e["short"].lower() in existing for e in entries):
            recovered.extend(entries)
        else:
            # Do not mark a partially encoded source as consumed; the normal
            # planner will rebuild it on the next run.
            break
    return recovered


def consumed_sources(manifest):
    return {entry.get("source") for entry in manifest}


def next_short_index(output_dir):
    pattern = re.compile(r"^s(\d+)\.mp4$", re.IGNORECASE)
    highest = 0
    if os.path.isdir(output_dir):
        for n in os.listdir(output_dir):
            m = pattern.match(n)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def probe_duration(ffprobe, path):
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(out.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None


def file_is_stable(path):
    try:
        size1 = os.path.getsize(path)
        time.sleep(STABILITY_CHECK_SECONDS)
        size2 = os.path.getsize(path)
        return size1 == size2
    except OSError:
        return False


def video_filter_chain(cfg):
    width = int(cfg.get("frame_width", 1080))
    height = int(cfg.get("frame_height", 1920))
    fps = cfg.get("fps", 30)
    fit = cfg.get("fit", "cover")
    if fit == "cover":
        return (
            f"fps={fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},format=yuv420p"
        )
    return (
        f"fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p"
    )


def build_cut_command(cfg, src, start, dur, out_path, ffmpeg_bin):
    cmd = [ffmpeg_bin, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    cmd += ["-vf", video_filter_chain(cfg)]
    if bool(cfg.get("keep_audio", True)):
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", str(cfg.get("video_codec", "libx264")),
        "-preset", str(cfg.get("preset", "veryfast")),
        "-crf", str(cfg.get("crf", 20)),
        "-threads", str(max(1, int(cfg.get("ffmpeg_threads", 2)))),
        "-filter_threads", str(max(1, int(cfg.get("filter_threads", 1)))),
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd


def process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe,
                    dry_run, force, watch, warned=None, max_shorts=None):
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    if warned is None:
        warned = set()

    short_seconds = float(cfg.get("short_seconds", 45))
    manifest = [] if force else load_manifest(output_dir)
    if not force and not manifest and os.listdir(output_dir):
        manifest = recover_manifest(output_dir, raw_dir, ffprobe, short_seconds)
        if manifest:
            save_manifest(output_dir, manifest)
            print(f"Recovered manifest for {len(manifest)} existing short(s).")
    done_sources = consumed_sources(manifest) if not force else set()
    skip = set(cfg.get("skip_files", []))
    pending = []
    for p in list_videos(raw_dir, strict=False):
        name = os.path.basename(p)
        if name in skip or name in done_sources:
            continue
        if file_is_stable(p):
            pending.append(p)
        elif p not in warned:
            print(f"(waiting: {name} is still being written)")

    if not pending:
        return manifest

    jobs = []
    planned = {}
    idx_counter = next_short_index(output_dir)
    for src in pending:
        dur = probe_duration(ffprobe, src) if ffprobe else None
        if dur is None:
            if src not in warned:
                print(f"SKIP {os.path.basename(src)}: cannot read this file "
                      f"(unfinished or corrupt?)")
                warned.add(src)
            continue
        name = os.path.basename(src)
        entries = []
        t = 0.0
        while t < dur:
            w = min(short_seconds, dur - t)
            out_name = f"s{idx_counter:03d}.mp4"
            idx_counter += 1
            jobs.append((src, t, w, os.path.join(output_dir, out_name)))
            entries.append({"short": out_name, "source": name,
                            "start": round(t, 3), "duration": round(w, 3)})
            t += w
            if max_shorts and len(jobs) >= max_shorts:
                break
        planned[name] = entries
        note = "" if dur % short_seconds == 0 else \
            f" (last one {dur - (len(entries) - 1) * short_seconds:.1f}s)"
        print(f"plan  {name}: {len(entries)} short(s){note}")
        if max_shorts and len(jobs) >= max_shorts:
            break

    total = len(jobs)
    done = [0]
    lock = threading.Lock()
    workers = max(1, int(cfg.get("max_workers", 1)))

    def run_cut(job):
        src, start, w, out_path = job
        if dry_run:
            return job, None
        cmd = build_cut_command(cfg, src, start, w, out_path, ffmpeg or "ffmpeg")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return job, proc

    failures, made_ok = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_cut, j) for j in jobs]
        for fut in as_completed(futures):
            (src, start, w, out_path), proc = fut.result()
            with lock:
                done[0] += 1
                label = os.path.basename(out_path)
                if proc is None:
                    print(f"  [{done[0]:>3}/{total}] (dry) {label} <- "
                          f"{os.path.basename(src)} @ {start:.0f}s ({w:.1f}s)")
                elif proc.returncode == 0 and os.path.exists(out_path):
                    print(f"  [{done[0]:>3}/{total}] {label} <- "
                          f"{os.path.basename(src)} @ {start:.0f}s ({w:.1f}s)")
                    made_ok.append(label)
                else:
                    failures.append((label, proc.stderr))

    if failures:
        label, err = failures[0]
        print(err[-2000:])
        die(f"ffmpeg failed for {label}")

    if not dry_run:
        for name, entries in planned.items():
            manifest.extend(e for e in entries if e["short"] in set(made_ok))
        save_manifest(output_dir, manifest)
    return manifest


def watch_loop(cfg, raw_dir, output_dir, ffmpeg, ffprobe, dry_run):
    print(f"Watching '{raw_dir}' -> '{output_dir}' "
          f"({cfg.get('short_seconds', 45)}s per short)")
    print("Drop footage in any time. Press Ctrl+C to stop.\n")
    warned = set()
    try:
        while True:
            process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe,
                            dry_run, False, True, warned)
            time.sleep(WATCH_POLL_SECONDS)
    except KeyboardInterrupt:
        manifest = load_manifest(output_dir)
        print(f"\nStopped. {len(manifest)} short(s) built so far in '{output_dir}'.")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args
    watch = "--watch" in args
    max_shorts = None
    if "--max-shorts" in args:
        try:
            max_shorts = max(1, int(args[args.index("--max-shorts") + 1]))
        except (ValueError, IndexError):
            die("--max-shorts requires a positive integer")

    cfg = load_config()
    raw_dir = os.path.join(HERE, cfg.get("raw_dir", "raw"))
    output_dir = os.path.join(HERE, cfg.get("output_dir", "shorts"))

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not dry_run:
        if not ffmpeg:
            die("ffmpeg was not found on your PATH. Install it first:\n"
                "  Windows: winget install ffmpeg\n"
                "  macOS:   brew install ffmpeg")
        ffmpeg = os.path.abspath(ffmpeg)
        if not ffprobe:
            die("ffprobe was not found on your PATH (it ships with ffmpeg).")
        ffprobe = os.path.abspath(ffprobe)

    if force:
        p = manifest_path(output_dir)
        if os.path.exists(p):
            os.remove(p)

    if watch:
        watch_loop(cfg, raw_dir, output_dir, ffmpeg, ffprobe, dry_run)
        return

    manifest = process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe,
                               dry_run, force, watch=False,
                               max_shorts=max_shorts)
    if dry_run:
        print("\nDRY RUN - nothing was written.")
    else:
        print(f"\nDone: {len(manifest)} short(s) in '{output_dir}'")


if __name__ == "__main__":
    main()
