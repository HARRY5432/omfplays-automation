#!/usr/bin/env python3
"""
build_shorts_omf.py — omfplays edition
Raw footage → 40s vertical shorts with horizontal→vertical flip:
  Horizontal upper half → Right side of vertical
  Horizontal lower half → Left side of vertical
"""
import json, os, re, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}

def die(msg): print(f"\nERROR: {msg}"); sys.exit(1)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f: return json.load(f)

def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

def list_videos(directory):
    if not os.path.isdir(directory): return []
    return sorted(
        [os.path.join(directory, n) for n in os.listdir(directory)
         if os.path.splitext(n)[1].lower() in VIDEO_EXT],
        key=lambda p: natural_key(os.path.basename(p))
    )

def manifest_path(output_dir): return os.path.join(output_dir, "manifest.json")

def load_manifest(output_dir):
    p = manifest_path(output_dir)
    if os.path.exists(p):
        try: return json.load(open(p, "r", encoding="utf-8"))
        except: return []
    return []

def save_manifest(output_dir, manifest):
    with open(manifest_path(output_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

def consumed_sources(manifest): return {e.get("source") for e in manifest}

def next_short_index(output_dir):
    pattern = re.compile(r"^s(\d+)\.mp4$", re.IGNORECASE)
    highest = 0
    if os.path.isdir(output_dir):
        for n in os.listdir(output_dir):
            m = pattern.match(n)
            if m: highest = max(highest, int(m.group(1)))
    return highest + 1

def probe_duration(ffprobe, path):
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "json", path], capture_output=True, text=True, timeout=120)
        return float(json.loads(out.stdout)["format"]["duration"])
    except: return None

def file_is_stable(path):
    try:
        s1 = os.path.getsize(path); time.sleep(2); s2 = os.path.getsize(path)
        return s1 == s2
    except: return False

def get_video_dimensions(ffprobe, path):
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "json", path],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout)
        if data.get("streams"):
            s = data["streams"][0]
            return int(s["width"]), int(s["height"])
    except: pass
    return None, None

def build_filter_chain(cfg, src_w, src_h):
    """
    Horizontal → Vertical flip (reversed):
    - Upper half of horizontal → LEFT side of vertical
    - Lower half of horizontal → RIGHT side of vertical
    This is a 90° clockwise rotation with split.
    """
    target_w = int(cfg.get("frame_width", 1080))
    target_h = int(cfg.get("frame_height", 1920))
    fps = cfg.get("fps", 30)

    if src_w > src_h:  # horizontal source
        # Split horizontal into top/bottom, stack vertically, then rotate 90° CW
        # Top half (upper) → LEFT side, Bottom half (lower) → RIGHT side
        # crop=w:h/2:0:0 (top), crop=w:h/2:0:h/2 (bottom)
        # vstack them → w x h, then transpose=1 (90° CW) → h x w = 1920x1080
        # then scale/crop to target 1080x1920
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
    else:  # already vertical or square
        fit = cfg.get("fit", "cover")
        if fit == "cover":
            return (f"fps={fps},scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h},format=yuv420p")
        return (f"fps={fps},scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p")

def build_cut_command(cfg, src, start, dur, out_path, ffmpeg, src_w, src_h):
    cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    cmd += ["-vf", build_filter_chain(cfg, src_w, src_h)]
    if bool(cfg.get("keep_audio", True)):
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", str(cfg.get("video_codec", "libx264")),
        "-preset", str(cfg.get("preset", "veryfast")),
        "-crf", str(cfg.get("crf", 22)),
        "-threads", str(max(1, int(cfg.get("ffmpeg_threads", 2)))),
        "-filter_threads", str(max(1, int(cfg.get("filter_threads", 1)))),
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd

def process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe, dry_run, force, max_shorts=None):
    os.makedirs(raw_dir, exist_ok=True); os.makedirs(output_dir, exist_ok=True)
    short_seconds = float(cfg.get("short_seconds", 40))
    manifest = [] if force else load_manifest(output_dir)
    done_sources = consumed_sources(manifest) if not force else set()
    skip = set(cfg.get("skip_files", []))
    pending = []
    for p in list_videos(raw_dir):
        name = os.path.basename(p)
        if name in skip or name in done_sources: continue
        if file_is_stable(p): pending.append(p)
        else: print(f"(waiting: {name} still being written)")
    if not pending: return manifest

    jobs = []; planned = {}; idx = next_short_index(output_dir)
    for src in pending:
        dur = probe_duration(ffprobe, src)
        if dur is None: print(f"SKIP {os.path.basename(src)}: cannot read duration"); continue
        src_w, src_h = get_video_dimensions(ffprobe, src)
        if not src_w: print(f"SKIP {os.path.basename(src)}: cannot read dimensions"); continue
        name = os.path.basename(src); entries = []; t = 0.0
        while t < dur:
            w = min(short_seconds, dur - t)
            out_name = f"s{idx:03d}.mp4"; idx += 1
            jobs.append((src, t, w, os.path.join(output_dir, out_name), src_w, src_h))
            entries.append({"short": out_name, "source": name, "start": round(t,3), "duration": round(w,3)})
            t += w
            if max_shorts and len(jobs) >= max_shorts: break
        planned[name] = entries
        print(f"plan  {name}: {len(entries)} short(s) ({src_w}x{src_h})")
        if max_shorts and len(jobs) >= max_shorts: break

    total = len(jobs); done = [0]; lock = threading.Lock()
    workers = max(1, int(cfg.get("max_workers", 1)))
    failures, made_ok = [], []

    def run_cut(job):
        src, start, w, out_path, src_w, src_h = job
        if dry_run: return job, None
        cmd = build_cut_command(cfg, src, start, w, out_path, ffmpeg, src_w, src_h)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return job, proc

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_cut, j) for j in jobs]
        for fut in as_completed(futures):
            (src, start, w, out_path, _, _), proc = fut.result()
            with lock:
                done[0] += 1
                label = os.path.basename(out_path)
                if proc is None:
                    print(f"  [{done[0]:>3}/{total}] (dry) {label} <- {os.path.basename(src)} @ {start:.0f}s ({w:.1f}s)")
                elif proc.returncode == 0 and os.path.exists(out_path):
                    print(f"  [{done[0]:>3}/{total}] {label} <- {os.path.basename(src)} @ {start:.0f}s ({w:.1f}s)")
                    made_ok.append(label)
                else:
                    failures.append((label, proc.stderr))

    if failures:
        label, err = failures[0]
        print(err[-2000:]); die(f"ffmpeg failed for {label}")

    if not dry_run:
        for name, entries in planned.items():
            manifest.extend(e for e in entries if e["short"] in set(made_ok))
        save_manifest(output_dir, manifest)
    return manifest

def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args; force = "--force" in args
    max_shorts = None
    if "--max-shorts" in args:
        try: max_shorts = max(1, int(args[args.index("--max-shorts")+1]))
        except: die("--max-shorts requires positive integer")

    cfg = load_config()
    raw_dir = os.path.join(HERE, cfg.get("raw_dir", "raw"))
    output_dir = os.path.join(HERE, cfg.get("output_dir", "shorts"))

    ffmpeg = shutil.which("ffmpeg"); ffprobe = shutil.which("ffprobe")
    if not dry_run:
        if not ffmpeg: die("ffmpeg not on PATH")
        if not ffprobe: die("ffprobe not on PATH")
        ffmpeg = os.path.abspath(ffmpeg); ffprobe = os.path.abspath(ffprobe)

    if force:
        p = manifest_path(output_dir)
        if os.path.exists(p): os.remove(p)

    manifest = process_pending(cfg, raw_dir, output_dir, ffmpeg, ffprobe, dry_run, force, max_shorts)
    if dry_run: print("\nDRY RUN - nothing written.")
    else: print(f"\nDone: {len(manifest)} short(s) in '{output_dir}'")

if __name__ == "__main__":
    main()