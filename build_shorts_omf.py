#!/usr/bin/env python3
"""
build_shorts_omf.py — Robust raw footage → vertical shorts builder.

Features:
  • Linear cap: max daily_shorts per run
  • Horizontal → vertical flip (upper→left, lower→right)
  • Atomic manifest writes
  • ffmpeg retry on transient failure
  • Partial file cleanup on failure
  • Idempotent — safe to re-run
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}
MAX_ENCODE_RETRIES = 2
RETRY_DELAY_SEC = 15


def die(msg: str):
    print(f"\nERROR: {msg}", flush=True)
    sys.exit(1)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die(f"config.json not found at {CONFIG_PATH}")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"config.json invalid JSON: {e}")


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_videos(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        [directory / n for n in os.listdir(directory)
         if Path(n).suffix.lower() in VIDEO_EXT],
        key=lambda p: natural_key(p.name)
    )


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def consumed_sources(manifest) -> set:
    return {e.get("source") for e in manifest if isinstance(e, dict)}


def next_short_index(output_dir: Path) -> int:
    pattern = re.compile(r"^s(\d+)\.mp4$", re.IGNORECASE)
    highest = 0
    if output_dir.is_dir():
        for n in os.listdir(output_dir):
            m = pattern.match(n)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def probe_duration(ffprobe: str, path: Path) -> float | None:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


def file_is_stable(path: Path) -> bool:
    try:
        s1 = os.path.getsize(path)
        time.sleep(2)
        s2 = os.path.getsize(path)
        return s1 == s2
    except OSError:
        return False


def get_video_dimensions(ffprobe: str, path: Path) -> tuple[int, int]:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout)
        if data.get("streams"):
            s = data["streams"][0]
            return int(s["width"]), int(s["height"])
    except Exception:
        pass
    return 0, 0


def build_filter_chain(cfg: dict, src_w: int, src_h: int) -> str:
    target_w = int(cfg.get("frame_width", 1080))
    target_h = int(cfg.get("frame_height", 1920))
    fps = cfg.get("fps", 30)

    if src_w > src_h:
        return (
            f"fps={fps},"
            f"split=2[top][bot],"
            f"[top]crop={src_w}:{src_h//2}:0:0[topc],"
            f"[bot]crop={src_w}:{src_h//2}:0:{src_h//2}[botc],"
            f"[topc][botc]vstack=inputs=2[stacked],"
            f"[stacked]transpose=1[rotated],"
            f"[rotated]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},format=yuv420p"
        )
    fit = cfg.get("fit", "cover")
    if fit == "cover":
        return (f"fps={fps},scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},format=yuv420p")
    return (f"fps={fps},scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p")


def build_cut_command(cfg: dict, src: Path, start: float, dur: float,
                      out_path: Path, ffmpeg: str, src_w: int, src_h: int) -> list[str]:
    cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    cmd += ["-vf", build_filter_chain(cfg, src_w, src_h)]
    if bool(cfg.get("keep_audio", True)):
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", str(cfg.get("video_codec", "libx264")),
        "-preset", str(cfg.get("preset", "fast")),
        "-crf", str(cfg.get("crf", 20)),
        "-threads", str(max(1, int(cfg.get("ffmpeg_threads", 0)))),
        "-filter_threads", str(max(1, int(cfg.get("filter_threads", 1)))),
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def run_cut_with_retry(cfg, src, start, dur, out_path, ffmpeg, src_w, src_h) -> bool:
    for attempt in range(1, MAX_ENCODE_RETRIES + 1):
        try:
            cmd = build_cut_command(cfg, src, start, dur, out_path, ffmpeg, src_w, src_h)
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300)
            if proc.returncode == 0 and out_path.exists():
                return True
            print(f"  ffmpeg attempt {attempt} failed (code {proc.returncode})", flush=True)
        except subprocess.TimeoutExpired:
            print(f"  ffmpeg attempt {attempt} timed out", flush=True)
        except Exception as e:
            print(f"  ffmpeg attempt {attempt} error: {e}", flush=True)

        # Cleanup partial output
        try:
            if out_path.exists():
                out_path.unlink()
        except Exception:
            pass

        if attempt < MAX_ENCODE_RETRIES:
            time.sleep(RETRY_DELAY_SEC)
    return False


def process_pending(cfg: dict, raw_dir: Path, output_dir: Path, ffmpeg: str,
                    ffprobe: str, dry_run: bool, max_shorts: int | None) -> list:
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    short_seconds = float(cfg.get("short_seconds", 40))
    manifest = [] if not dry_run else load_json(output_dir / "manifest.json", [])
    done_sources = consumed_sources(manifest)
    skip = set(cfg.get("skip_files", []))

    pending = []
    for p in list_videos(raw_dir):
        name = p.name
        if name in skip or name in done_sources:
            continue
        if file_is_stable(p):
            pending.append(p)
        else:
            print(f"(waiting: {name} still being written)", flush=True)

    if not pending:
        print("No pending raw videos.", flush=True)
        return manifest

    # Build jobs
    jobs = []
    planned = {}
    idx = next_short_index(output_dir)
    for src in pending:
        dur = probe_duration(ffprobe, src)
        if dur is None:
            print(f"SKIP {src.name}: cannot read duration", flush=True)
            continue
        src_w, src_h = get_video_dimensions(ffprobe, src)
        if not src_w:
            print(f"SKIP {src.name}: cannot read dimensions", flush=True)
            continue
        name = src.name
        entries = []
        t = 0.0
        while t < dur:
            w = min(short_seconds, dur - t)
            out_name = f"s{idx:03d}.mp4"
            idx += 1
            jobs.append((src, t, w, output_dir / out_name, src_w, src_h))
            entries.append({"short": out_name, "source": name,
                            "start": round(t, 3), "duration": round(w, 3)})
            t += w
            if max_shorts and len(jobs) >= max_shorts:
                break
        planned[name] = entries
        print(f"plan  {name}: {len(entries)} short(s) ({src_w}x{src_h})", flush=True)
        if max_shorts and len(jobs) >= max_shorts:
            break

    total = len(jobs)
    done = [0]
    lock = threading.Lock()
    workers = max(1, int(cfg.get("max_workers", 1)))
    failures = []
    made_ok = []

    def run_cut(job):
        src, start, w, out_path, src_w, src_h = job
        if dry_run:
            return job, True
        success = run_cut_with_retry(cfg, src, start, w, out_path, ffmpeg, src_w, src_h)
        return job, success

    print(f"Encoding {total} short(s) with {workers} worker(s)...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_cut, j) for j in jobs]
        for fut in as_completed(futures):
            (src, start, w, out_path, _, _), success = fut.result()
            with lock:
                done[0] += 1
                label = out_path.name
                if success:
                    print(f"  [{done[0]:>3}/{total}] ✅ {label} <- {src.name} @ {start:.0f}s", flush=True)
                    made_ok.append(label)
                else:
                    failures.append((label, src.name))

    if failures:
        print("\n❌ Failed encodes:", flush=True)
        for label, src_name in failures:
            print(f"    {label} (from {src_name})", flush=True)
        die(f"{len(failures)} short(s) failed to encode.")

    if not dry_run:
        final_manifest = []
        for name, entries in planned.items():
            for e in entries:
                if e["short"] in set(made_ok):
                    final_manifest.append(e)
        atomic_write_json(output_dir / "manifest.json", final_manifest)
        print(f"\n✅ {len(made_ok)} short(s) encoded successfully.", flush=True)

    return manifest


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args
    max_shorts = None

    if "--max-shorts" in args:
        try:
            max_shorts = max(1, int(args[args.index("--max-shorts") + 1]))
        except Exception:
            die("--max-shorts requires a positive integer")

    cfg = load_config()
    raw_dir = HERE / cfg.get("raw_dir", "raw")
    output_dir = HERE / cfg.get("output_dir", "shorts")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not dry_run:
        if not ffmpeg:
            die("ffmpeg not found on PATH. Install: winget install ffmpeg")
        if not ffprobe:
            die("ffprobe not found on PATH (ships with ffmpeg).")
        ffmpeg = os.path.abspath(ffmpeg)
        ffprobe = os.path.abspath(ffprobe)

    if force:
        p = output_dir / "manifest.json"
        if p.exists():
            p.unlink()

    # Linear cap: daily_shorts
    daily_limit = int(cfg.get("daily_shorts", 5))
    existing_shorts = len(list(output_dir.glob("s*.mp4"))) if not force else 0
    remaining = daily_limit - existing_shorts
    if remaining <= 0:
        print(f"Already have {existing_shorts} shorts today (limit {daily_limit}). Skipping build.", flush=True)
        return
    if max_shorts is None or max_shorts > remaining:
        max_shorts = remaining

    manifest = process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe, dry_run, max_shorts)
    if dry_run:
        print("\nDRY RUN - nothing written.", flush=True)
    else:
        print(f"\nDone: {len(manifest)} short(s) in '{output_dir}'", flush=True)


if __name__ == "__main__":
    main()