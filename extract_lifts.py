#!/usr/bin/env python3
"""
Extract powerlifting lifts from a YouTube competition video and create
an Instagram-compatible combined video with 3 lifts stacked vertically.

Modes:
  - Interactive : run without arguments, prompts for every input with defaults
  - Parameter   : pass URL as first argument; timestamps via --times FILE
                  or inline with --timestamps t1 t2 ... t9

Requirements: yt-dlp, ffmpeg
"""

import argparse
import json
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MOVEMENTS = [
    "squat", "squat", "squat",
    "bench", "bench", "bench",
    "deadlift", "deadlift", "deadlift",
]
DEFAULT_DURATION = 60
DEFAULT_OUTPUT_DIR = "lifts"
DEFAULT_TIMES_FILE = "times.txt"


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def parse_timestamp(ts: str) -> int:
    """Parse mixed-format timestamp string to total seconds.

    Handles: HH:MM:SS, H:MM:SS, MM:SS, XhMM:SS, Xh:MM:SS
    Examples: '0:21:27', '1h23:30', '2h33:4'
    """
    ts = ts.strip()
    ts = re.sub(r'^(\d+)h:?(\d+):(\d+)$', r'\1:\2:\3', ts)  # normalise XhMM:SS
    parts = ts.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    raise ValueError(f"Cannot parse timestamp: '{ts}'")


def seconds_to_hms(total: int) -> str:
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_timestamps_file(path: Path) -> list[int]:
    """Read exactly 9 timestamps from a file (one per line, blank lines skipped)."""
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    if len(lines) != 9:
        sys.exit(f"Error: expected 9 timestamps in '{path}', found {len(lines)}")
    return [parse_timestamp(l) for l in lines]


# ── Interactive prompt helpers ─────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    """Show a prompt with an optional default; pressing Enter returns the default."""
    hint = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{hint}: ").strip()
        if answer:
            return answer
        if default:
            return default
        print("  (this field is required, please enter a value)")


def prompt_int(question: str, default: int, min_val: int = 1, max_val: int = 9999) -> int:
    while True:
        raw = prompt(question, str(default))
        try:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  (enter a number between {min_val} and {max_val})")
        except ValueError:
            print("  (enter a valid integer)")


def prompt_bool(question: str, default: bool = True) -> bool:
    default_str = "yes" if default else "no"
    raw = prompt(f"{question} (yes/no)", default_str).lower()
    return raw in ("y", "yes", "s", "si", "sí", "1", "true")


def prompt_choice(question: str, choices: list[str], default: str) -> str:
    opts = "/".join(choices)
    while True:
        raw = prompt(f"{question} ({opts})", default)
        if raw in choices:
            return raw
        print(f"  (choose one of: {opts})")


def prompt_timestamps_manual() -> list[int]:
    """Prompt user to enter each of the 9 lift timestamps individually."""
    labels = [
        "Squat 1", "Squat 2", "Squat 3",
        "Bench 1", "Bench 2", "Bench 3",
        "Deadlift 1", "Deadlift 2", "Deadlift 3",
    ]
    print("  Enter timestamps in H:MM:SS or XhMM:SS format:")
    timestamps = []
    for label in labels:
        while True:
            raw = input(f"    {label}: ").strip()
            try:
                timestamps.append(parse_timestamp(raw))
                break
            except ValueError as e:
                print(f"    Error: {e} — try again")
    return timestamps


# ── Core download / assembly logic ─────────────────────────────────────────────

def get_clip_duration(path: Path) -> float:
    """Return the real duration of a video file in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def download_clip(url: str, start: int, duration: int, output: Path, label: str) -> None:
    """Download a single timed section from YouTube using yt-dlp."""
    end = start + duration
    section = f"*{seconds_to_hms(start)}-{seconds_to_hms(end)}"
    print(f"\n[{label}] ⏳ Descargando...  {seconds_to_hms(start)} → {seconds_to_hms(end)}")
    sys.stdout.flush()

    tmp = output.with_suffix(".tmp.mp4")
    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "-f", "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-N", "1",                             # sequential fragments → gradual progress
        "--newline",                           # one progress line per update (no \r)
        "-o", str(tmp),
        "--no-playlist",
        url,
    ]

    if sys.stdout.isatty():
        # CLI: yt-dlp writes directly to the terminal → real-time native progress bar
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
    else:
        # Web (stdout = _LiveLog pipe): use a pty so yt-dlp thinks it has a real
        # terminal and doesn't suppress intermediate progress output.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(cmd, stdout=slave_fd, stderr=slave_fd,
                                close_fds=True, env=env)
        os.close(slave_fd)

        last_boundary = -1
        output_lines: list[str] = []
        buf = b""
        start_t = time.monotonic()
        last_tick_t = start_t
        TICK_INTERVAL = 5.0

        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.2)
            except (ValueError, OSError):
                break
            if r:
                try:
                    buf += os.read(master_fd, 4096)
                except OSError:
                    break
                lines = buf.split(b'\n')
                buf = lines[-1]
                for raw in lines[:-1]:
                    line = raw.replace(b'\r', b'').decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if "[download]" in line and "%" in line:
                        try:
                            pct = float(next(t for t in line.split()
                                            if t.endswith("%")).rstrip("%"))
                            boundary = int(pct // 5) * 5
                            if boundary > last_boundary:
                                last_boundary = boundary
                                last_tick_t = time.monotonic()  # reset tick
                                print(f"  ↓ {boundary}%")
                                sys.stdout.flush()
                        except (ValueError, StopIteration):
                            pass
            # Time-based tick: if yt-dlp emits no % progress (e.g. ffmpeg backend),
            # print elapsed seconds every TICK_INTERVAL so the log stays alive.
            now = time.monotonic()
            if now - last_tick_t >= TICK_INTERVAL:
                elapsed = int(now - start_t)
                print(f"  ↓ {elapsed}s...")
                sys.stdout.flush()
                last_tick_t = now
            if proc.poll() is not None:
                # Drain any remaining output after process exits
                try:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.05)
                        if not r:
                            break
                        buf += os.read(master_fd, 4096)
                except OSError:
                    pass
                break

        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait()
        if proc.returncode != 0:
            for line in output_lines:
                if line and "[download]" not in line:
                    print(line)
            raise subprocess.CalledProcessError(proc.returncode, cmd)

    # Apply faststart for faster browser playback (stream-copy, lossless).
    # If ffmpeg can't process the file (e.g. unsupported codec build), fall back to the raw file.
    faststart_cmd = [
        "ffmpeg", "-i", str(tmp),
        "-c", "copy", "-movflags", "+faststart",
        "-y", str(output),
    ]
    r = subprocess.run(faststart_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        tmp.unlink()
        print("✓ Clip listo")
    else:
        tmp.rename(output)
        print("✓ Clip listo (sin optimizar para streaming)")


def make_combined(clips: list[Path], output: Path, preview_width: int = 0) -> None:
    """Stack three clips vertically with ffmpeg vstack (no audio).

    Shorter clips are frozen on their last frame until the longest one ends.
    If preview_width > 0, each clip is scaled to that width before stacking.
    """
    real_durations = [get_clip_duration(c) for c in clips]
    max_dur = max(real_durations)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    # Build per-stream filter: optional scale + tpad freeze to match longest clip
    labels = ["a", "b", "c"]
    parts = []
    for i, (dur, lbl) in enumerate(zip(real_durations, labels)):
        pad = max_dur - dur
        scale = f"scale={preview_width}:-2," if preview_width else ""
        parts.append(f"[{i}:v]{scale}tpad=stop_mode=clone:stop_duration={pad:.3f}[{lbl}]")

    filter_complex = ";".join(parts) + ";[a][b][c]vstack=inputs=3[v]"

    cmd = [
        "ffmpeg",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-color_primaries", "bt709",           # preserve colour metadata through vstack re-encode
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-an",                                 # no audio in combined video
        "-movflags", "+faststart",
        "-y",
        str(output),
    ]
    print(f"\n[combined] ⏳ Creando vídeo combinado...")
    sys.stdout.flush()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("✓ Vídeo combinado listo")


def make_preview(source: Path, dest: Path, width: int) -> None:
    """Generate a low-resolution copy of a clip for local preview."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", str(source),
        "-vf", f"scale={width}:-2",
        "-c:v", "libx264", "-crf", "28", "-preset", "fast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-y", str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Music helpers ──────────────────────────────────────────────────────────────

def search_youtube(query: str, n: int = 5) -> list[dict]:
    """Search YouTube and return up to n results as dicts with title/channel/duration/url."""
    print(f"  Searching YouTube for: {query!r} ...")
    result = subprocess.run(
        ["yt-dlp", f"ytsearch{n}:{query}", "--flat-playlist", "-j", "--quiet"],
        capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        d = json.loads(line)
        vid_id = d.get("id", "")
        entries.append({
            "title":    d.get("title", "Unknown"),
            "channel":  d.get("channel") or d.get("uploader", "Unknown"),
            "duration": d.get("duration_string") or d.get("duration", "?"),
            "url":      f"https://www.youtube.com/watch?v={vid_id}",
        })
    return entries


def download_audio(url: str, dest: Path) -> None:
    """Download the best available audio from a YouTube URL as m4a."""
    print(f"  Downloading audio from: {url}")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "-x", "--audio-format", "m4a",
        "--no-playlist",
        "-o", str(dest),
        url,
    ]
    subprocess.run(cmd, check=True)


def add_music(
    video: Path,
    audio: Path,
    output: Path,
    music_start: float = 0.0,
    fade_secs: float = 2.0,
) -> None:
    """Mix an audio track into a silent video file.

    music_start: seconds into the song to begin playback (adjusted automatically
                 if the remaining song length is shorter than the video).
    The audio is looped if shorter than the video, trimmed if longer, and
    faded out over the last fade_secs seconds. The video stream is copied
    without re-encoding.
    """
    video_dur = get_clip_duration(video)
    song_dur  = get_clip_duration(audio)

    # Clamp start so there is always enough song to cover the video
    max_start = max(0.0, song_dur - video_dur)
    if music_start > max_start:
        print(f"  ℹ  Music start adjusted: {music_start:.1f}s → {max_start:.1f}s "
              f"(song ends at {song_dur:.1f}s, video needs {video_dur:.1f}s)")
        music_start = max_start

    end_trim  = music_start + video_dur
    fade_start = max(0.0, video_dur - fade_secs)

    audio_filter = (
        f"[1:a]aloop=loop=-1:size=2147483647,"             # loop in case song < video
        f"atrim=start={music_start:.3f}:end={end_trim:.3f},"  # pick the chosen window
        f"asetpts=PTS-STARTPTS,"                            # reset timestamps after trim
        f"volume=0.85,"
        f"afade=t=out:st={fade_start:.3f}:d={fade_secs}[aout]"
    )
    cmd = [
        "ffmpeg",
        "-i", str(video),
        "-i", str(audio),
        "-filter_complex", audio_filter,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",                            # lossless: no video re-encode
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", str(output),
    ]
    print(f"\n[music] ⏳ Aplicando música...")
    sys.stdout.flush()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("✓ Música aplicada")


def add_mixed_audio(
    clip: Path,
    audio: Path,
    output: Path,
    music_start: float = 0.0,
    music_pct: int = 40,
    fade_secs: float = 2.0,
) -> None:
    """Blend a clip's original audio with a music track.

    music_pct: percentage of music in the final mix (10-90). Original gets
    (100 - music_pct)%. normalize=0 so the explicit volumes are honoured exactly.
    """
    music_pct = max(10, min(90, music_pct))
    orig_vol  = (100 - music_pct) / 100
    music_vol = music_pct / 100

    video_dur = get_clip_duration(clip)
    song_dur  = get_clip_duration(audio)

    max_start = max(0.0, song_dur - video_dur)
    if music_start > max_start:
        print(f"  ℹ  Music start adjusted: {music_start:.1f}s → {max_start:.1f}s "
              f"(song ends at {song_dur:.1f}s, video needs {video_dur:.1f}s)")
        music_start = max_start

    end_trim   = music_start + video_dur
    fade_start = max(0.0, video_dur - fade_secs)

    audio_filter = (
        f"[1:a]aloop=loop=-1:size=2147483647,"
        f"atrim=start={music_start:.3f}:end={end_trim:.3f},"
        f"asetpts=PTS-STARTPTS,"
        f"volume={music_vol:.2f},"
        f"afade=t=out:st={fade_start:.3f}:d={fade_secs}[music_proc];"
        f"[0:a]volume={orig_vol:.2f}[orig_proc];"
        f"[orig_proc][music_proc]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    cmd = [
        "ffmpeg",
        "-i", str(clip),
        "-i", str(audio),
        "-filter_complex", audio_filter,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", str(output),
    ]
    print(f"\n[mixed] ⏳ Mezclando audio original y música...")
    sys.stdout.flush()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("✓ Audio mezclado")


def resolve_music(source: str, interactive: bool = True) -> str:
    """Return a YouTube URL from either a direct URL or a search query.

    In interactive mode the user picks from a list of results.
    In CLI mode the first result is auto-selected (use a URL for full control).
    """
    if source.startswith("http://") or source.startswith("https://"):
        return source

    # Search mode
    results = search_youtube(source)
    if not results:
        sys.exit("Error: no YouTube results found for that search query.")

    print()
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r['title']}")
        print(f"     {r['channel']}  —  {r['duration']}  —  {r['url']}")
    print()

    if not interactive:
        print(f"  → Auto-selecting result 1 (pass a URL with --music for full control)")
        return results[0]["url"]

    while True:
        raw = input(f"Choose a result (1–{len(results)}), or paste a different URL: ").strip()
        if raw.startswith("http"):
            return raw
        try:
            idx = int(raw)
            if 1 <= idx <= len(results):
                return results[idx - 1]["url"]
        except ValueError:
            pass
        print(f"  (enter a number between 1 and {len(results)}, or a URL)")


def run(
    url: str,
    timestamps: list[int],
    durations: dict[str, int],   # keys: "squat", "bench", "deadlift"
    squat_attempt: int,
    bench_attempt: int,
    deadlift_attempt: int,
    output_dir: Path,
    skip_individual: bool,
    skip_combined: bool,
    preview_width: int,   # 0 = no preview
    no_replay: bool = False,
    music_source: str = "",   # YouTube URL or search query; empty = no music
    music_start: float = 0.0,
    interactive: bool = False,
    dry_run: bool = False,   # skip all network/ffmpeg calls; write placeholder files
) -> None:
    if no_replay:
        # Without a slow-motion replay, lifts are roughly half as long
        durations = {k: max(10, v // 2) for k, v in durations.items()}
    output_dir.mkdir(parents=True, exist_ok=True)

    clip_paths: list[Path] = []
    for i, movement in enumerate(MOVEMENTS, 1):
        attempt = ((i - 1) % 3) + 1
        clip_paths.append(output_dir / f"lift_{i:02d}_{movement}_attempt{attempt}.mp4")

    if not skip_individual:
        action = "Simulating" if dry_run else "Downloading"
        print(f"\n{action} {len(timestamps)} clips into '{output_dir}/'...")
        for i, (ts, path) in enumerate(zip(timestamps, clip_paths), 1):
            movement = MOVEMENTS[i - 1]
            attempt = ((i - 1) % 3) + 1
            duration = durations[movement]
            if dry_run:
                end = ts + duration
                print(f"\n  [lift {i:02d} — {movement} attempt {attempt}]"
                      f"  {seconds_to_hms(ts)} → {seconds_to_hms(end)}  [dry run]")
                path.write_bytes(b"")
            else:
                download_clip(url, ts, duration, path, f"lift {i:02d} — {movement} attempt {attempt}")
                if preview_width:
                    prev = output_dir / "preview" / path.name
                    print(f"    → preview {prev.name}")
                    make_preview(path, prev, preview_width)
    else:
        missing = [p for p in clip_paths if not p.exists()]
        if missing:
            sys.exit("Missing clips (run without --skip-individual first):\n" +
                     "\n".join(f"  {p}" for p in missing))
        if preview_width and not dry_run:
            print(f"\nGenerating previews from existing clips...")
            for path in clip_paths:
                prev = output_dir / "preview" / path.name
                print(f"  {path.name} → preview/{path.name}")
                make_preview(path, prev, preview_width)

    if skip_combined:
        print(f"\nDone. Clips saved to: {output_dir}/")
        return

    selected = [
        clip_paths[squat_attempt - 1],          # squat:    index 0–2
        clip_paths[3 + bench_attempt - 1],       # bench:    index 3–5
        clip_paths[6 + deadlift_attempt - 1],    # deadlift: index 6–8
    ]
    base = f"combined_s{squat_attempt}_b{bench_attempt}_d{deadlift_attempt}"

    # When music will be added we label the silent version explicitly for Instagram
    if music_source:
        combined_path = output_dir / f"{base}_for-instagram.mp4"
    else:
        combined_path = output_dir / f"{base}.mp4"

    if dry_run:
        print(f"\n  [combined]  {combined_path.name}  [dry run]")
        combined_path.write_bytes(b"")
    else:
        make_combined(selected, combined_path)
        if preview_width:
            prev_combined = output_dir / "preview" / combined_path.name
            print(f"  → combined preview {prev_combined.name}")
            make_combined(selected, prev_combined, preview_width=preview_width)

    # Music: resolve source → download audio → mix into a second combined file
    music_path: Path | None = None
    if music_source:
        if dry_run:
            music_path = output_dir / f"{base}_with-music.mp4"
            print(f"\n  [music]  {music_path.name}  [dry run]")
            music_path.write_bytes(b"")
        else:
            music_url = resolve_music(music_source, interactive=interactive)
            with tempfile.TemporaryDirectory() as tmp:
                audio_file = Path(tmp) / "music.m4a"
                download_audio(music_url, audio_file)
                music_path = output_dir / f"{base}_with-music.mp4"
                add_music(combined_path, audio_file, music_path, music_start=music_start)
                if preview_width:
                    prev_music = output_dir / "preview" / music_path.name
                    print(f"  → music preview {prev_music.name}")
                    make_preview(music_path, prev_music, preview_width)

    print(f"\n{'='*52}")
    print(f"  Individual clips   : {output_dir}/")
    if preview_width:
        print(f"  Previews (low-res) : {output_dir}/preview/")
    print(f"  Combined (Instagram): {combined_path}")
    if music_path:
        print(f"  Combined (with music): {music_path}")
        print()
        print("  ⚠  Do NOT upload the music version to Instagram (posts, reels or")
        print("     stories — all are scanned). Use the for-instagram file instead")
        print("     and add music directly in the Instagram app.")
    print(f"{'='*52}")


def run_single(
    url: str,
    timestamp: int,
    duration: int,
    movement: str,
    attempt: int,
    output_dir: Path,
    audio_mode: str,
    preview_width: int = 0,
    no_replay: bool = False,
    music_source: str = "",
    music_start: float = 0.0,
    music_pct: int = 40,
    interactive: bool = False,
    dry_run: bool = False,
) -> None:
    """Extract one lift clip with configurable audio.

    audio_mode:
      "original"   → 1 file, original audio (no copyright risk)
      "music_only" → 1 file, music replaces original audio
      "mixed"      → 3 files: original, original+music blend, music-only
    """
    if no_replay:
        duration = max(10, duration // 2)
    output_dir.mkdir(parents=True, exist_ok=True)

    base      = f"{movement}_attempt{attempt}"
    clip_path = output_dir / f"{base}_original.mp4"

    action = "Simulating" if dry_run else "Downloading"
    print(f"\n{action} single {movement} clip (attempt {attempt}) into '{output_dir}/'...")

    if dry_run:
        end = timestamp + duration
        print(f"\n  [{movement} attempt {attempt}]  "
              f"{seconds_to_hms(timestamp)} → {seconds_to_hms(end)}  [dry run]")
        clip_path.write_bytes(b"")
    else:
        download_clip(url, timestamp, duration, clip_path,
                      f"{movement} attempt {attempt}")
        if preview_width:
            make_preview(clip_path, output_dir / "preview" / clip_path.name, preview_width)

    if audio_mode == "original":
        print(f"\n{'='*52}")
        print(f"  Clip: {clip_path}")
        print(f"{'='*52}")
        return

    music_path = output_dir / f"{base}_music.mp4"
    mixed_path = output_dir / f"{base}_mixed.mp4"

    if dry_run:
        print(f"\n  [music]  {music_path.name}  [dry run]")
        music_path.write_bytes(b"")
        if audio_mode == "mixed":
            print(f"\n  [mixed]  {mixed_path.name}  [dry run]")
            mixed_path.write_bytes(b"")
    else:
        music_url = resolve_music(music_source, interactive=interactive)
        with tempfile.TemporaryDirectory() as tmp:
            audio_file = Path(tmp) / "music.m4a"
            download_audio(music_url, audio_file)
            add_music(clip_path, audio_file, music_path, music_start=music_start)
            if audio_mode == "mixed":
                add_mixed_audio(clip_path, audio_file, mixed_path,
                                music_start=music_start, music_pct=music_pct)
            if preview_width:
                make_preview(music_path, output_dir / "preview" / music_path.name, preview_width)
                if audio_mode == "mixed":
                    make_preview(mixed_path, output_dir / "preview" / mixed_path.name, preview_width)

    print(f"\n{'='*52}")
    print(f"  Clip (original audio): {clip_path}")
    if audio_mode in ("music_only", "mixed"):
        print(f"  Clip (music only):     {music_path}")
    if audio_mode == "mixed":
        print(f"  Clip (mixed audio):    {mixed_path}")
    if audio_mode in ("music_only", "mixed"):
        print()
        print("  ⚠  Do NOT upload the music or mixed versions to Instagram.")
    print(f"{'='*52}")


# ── Modes ──────────────────────────────────────────────────────────────────────

def interactive_mode() -> None:
    print("╔══════════════════════════════════════════════╗")
    print("║      Powerlifting Clip Extractor             ║")
    print("╚══════════════════════════════════════════════╝")
    print("(Press Enter to accept the value shown in [brackets])\n")

    url = prompt("YouTube URL")

    # Timestamps: from file or manual entry
    default_file = DEFAULT_TIMES_FILE if Path(DEFAULT_TIMES_FILE).exists() else ""
    if default_file:
        print(f"\nTimestamps: press Enter to load '{default_file}', or type another path / 'manual'.")
    else:
        print("\nTimestamps: enter a file path or 'manual' to type them one by one.")

    raw = input(f"Timestamps file{' [' + default_file + ']' if default_file else ''} (or 'manual'): ").strip()

    if raw.lower() == "manual":
        timestamps = prompt_timestamps_manual()
    elif raw:
        timestamps = load_timestamps_file(Path(raw))
        print(f"  Loaded {len(timestamps)} timestamps from '{raw}'")
    elif default_file:
        timestamps = load_timestamps_file(Path(default_file))
        print(f"  Loaded {len(timestamps)} timestamps from '{default_file}'")
    else:
        timestamps = prompt_timestamps_manual()

    print("\nClip duration in seconds (one per movement, Enter to keep same value):")
    dur_default = prompt_int("  Default for all", DEFAULT_DURATION, min_val=10, max_val=300)
    dur_squat    = prompt_int("  Squat",    dur_default, min_val=10, max_val=300)
    dur_bench    = prompt_int("  Bench",    dur_default, min_val=10, max_val=300)
    dur_deadlift = prompt_int("  Deadlift", dur_default, min_val=10, max_val=300)
    durations = {"squat": dur_squat, "bench": dur_bench, "deadlift": dur_deadlift}

    output_dir = Path(prompt("Output directory", DEFAULT_OUTPUT_DIR))

    print("\nWhich attempt to include in the combined video?")
    print("  (1 = first attempt, 2 = second, 3 = last — one per movement)")
    squat    = int(prompt_choice("  Squat",    ["1", "2", "3"], "3"))
    bench    = int(prompt_choice("  Bench",    ["1", "2", "3"], "3"))
    deadlift = int(prompt_choice("  Deadlift", ["1", "2", "3"], "3"))

    skip_ind  = not prompt_bool("\nDownload individual clips?", default=True)
    skip_comb = not prompt_bool("Create combined video?",      default=True)
    want_prev = prompt_bool("Generate low-res previews? (useful for slow devices)", default=False)
    prev_width = 640 if want_prev else 0

    music_source = ""
    music_start  = 0.0
    if not skip_comb:
        print()
        if prompt_bool("Add music to the combined video?", default=False):
            print()
            print("  Recommended: paste a YouTube URL (you choose the exact track).")
            print("  Alternative: type a search query and we'll show you 5 results.")
            print("  Note: the Instagram-safe version (no music) will always be generated too.")
            print()
            music_source = input("  YouTube URL or search query: ").strip()
            print()
            print("  From which point in the song should it start?")
            print("  Format: MM:SS or H:MM:SS — leave blank to start from the beginning.")
            print("  If the remaining song is too short, the start will be adjusted automatically.")
            raw_start = input("  Music start [0:00]: ").strip()
            try:
                music_start = float(parse_timestamp(raw_start)) if raw_start else 0.0
            except ValueError:
                print("  (invalid time, defaulting to beginning)")
                music_start = 0.0
        else:
            music_start = 0.0

    print()
    run(url, timestamps, durations, squat, bench, deadlift, output_dir,
        skip_ind, skip_comb, prev_width, music_source=music_source,
        music_start=music_start, interactive=True)


def cli_mode(args: argparse.Namespace) -> None:
    music_start = parse_timestamp(args.music_start) if args.music_start != "0" else 0

    if args.single:
        if not args.timestamp:
            sys.exit("Error: --single requires --timestamp TS")
        if args.movement not in ("squat", "bench", "deadlift"):
            sys.exit("Error: --movement must be squat, bench or deadlift")
        if args.audio_mode not in ("original", "music_only", "mixed"):
            sys.exit("Error: --audio-mode must be original, music_only or mixed")
        if args.audio_mode in ("music_only", "mixed") and not args.music:
            sys.exit("Error: --audio-mode music_only/mixed requires --music URL_OR_QUERY")
        run_single(
            url=args.url,
            timestamp=parse_timestamp(args.timestamp),
            movement=args.movement,
            attempt=args.attempt,
            duration=args.duration,
            output_dir=Path(args.output_dir),
            audio_mode=args.audio_mode,
            preview_width=args.preview_width,
            no_replay=args.no_replay,
            music_source=args.music or "",
            music_start=float(music_start),
            music_pct=max(10, min(90, args.mix_pct)),
            interactive=True,
        )
        return

    if args.timestamps:
        if len(args.timestamps) != 9:
            sys.exit(f"Error: --timestamps requires exactly 9 values, got {len(args.timestamps)}")
        timestamps = [parse_timestamp(t) for t in args.timestamps]
    else:
        times_path = Path(args.times)
        if not times_path.exists():
            sys.exit(f"Error: timestamps file '{times_path}' not found. "
                     f"Use --times PATH or --timestamps t1..t9")
        timestamps = load_timestamps_file(times_path)

    durations = {
        "squat":    args.duration_squat    or args.duration,
        "bench":    args.duration_bench    or args.duration,
        "deadlift": args.duration_deadlift or args.duration,
    }

    run(
        url=args.url,
        timestamps=timestamps,
        durations=durations,
        squat_attempt=args.squat,
        bench_attempt=args.bench,
        deadlift_attempt=args.deadlift,
        output_dir=Path(args.output_dir),
        skip_individual=args.skip_individual,
        skip_combined=args.skip_combined,
        preview_width=args.preview_width,
        no_replay=args.no_replay,
        music_source=args.music or "",
        music_start=float(music_start),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_lifts.py",
        description="Extract powerlifting competition lifts from YouTube and create a combined Instagram video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Interactive mode — prompts for everything
  python extract_lifts.py

  # Parameter mode — timestamps from file (default: times.txt)
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao

  # Parameter mode — timestamps inline
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao \\
      --timestamps 0:21:27 0:29:55 0:38:15 1h23:30 1h32:21 1h41:30 2h26:15 2h33:4 2h41:35

  # Custom attempt selection for combined video
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao --squat 2 --bench 3 --deadlift 3

  # Only recreate the combined video from already-downloaded clips
  python extract_lifts.py https://youtube.com/live/I3LHqLA8Xao --skip-individual

  # Single lift — original audio only (no copyright risk)
  python extract_lifts.py https://youtube.com/live/VIDEO_ID \\
      --single --timestamp 2h26:15 --movement deadlift --attempt 2

  # Single lift — mixed audio (original + music blended, 3 output files)
  python extract_lifts.py https://youtube.com/live/VIDEO_ID \\
      --single --timestamp 2h26:15 --movement deadlift --attempt 2 \\
      --audio-mode mixed --music "https://www.youtube.com/watch?v=..."
        """,
    )
    parser.add_argument("url", nargs="?",
                        help="YouTube URL (omit to run in interactive mode)")
    parser.add_argument("--times", metavar="FILE", default=DEFAULT_TIMES_FILE,
                        help=f"File with 9 timestamps, one per line (default: {DEFAULT_TIMES_FILE})")
    parser.add_argument("--timestamps", nargs=9, metavar="TS",
                        help="9 timestamps inline, e.g. 0:21:27 1h23:30 ... (overrides --times)")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, metavar="SECS",
                        help=f"Clip duration in seconds for all movements (default: {DEFAULT_DURATION})")
    parser.add_argument("--duration-squat", type=int, default=0, metavar="SECS",
                        help="Clip duration for squats (overrides --duration)")
    parser.add_argument("--duration-bench", type=int, default=0, metavar="SECS",
                        help="Clip duration for bench press (overrides --duration)")
    parser.add_argument("--duration-deadlift", type=int, default=0, metavar="SECS",
                        help="Clip duration for deadlifts (overrides --duration)")
    parser.add_argument("--squat",    type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Squat attempt for combined video (default: 3)")
    parser.add_argument("--bench",    type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Bench attempt for combined video (default: 3)")
    parser.add_argument("--deadlift", type=int, default=3, choices=[1, 2, 3], metavar="{1,2,3}",
                        help="Deadlift attempt for combined video (default: 3)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="DIR",
                        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}/)")
    parser.add_argument("--skip-individual", action="store_true",
                        help="Skip downloads, use existing clips in --output-dir")
    parser.add_argument("--skip-combined", action="store_true",
                        help="Download individual clips only, skip combined video")
    parser.add_argument("--preview", dest="preview_width", nargs="?", const=640,
                        type=int, default=0, metavar="WIDTH",
                        help="Generate low-res previews in <output-dir>/preview/ (default width: 640px)")
    parser.add_argument(
        "--music-start", metavar="TIME", default="0",
        help=(
            "Start the music from this point in the song (e.g. '1:30' or '0:45'). "
            "Default: beginning of the song. "
            "If the remaining song length from this point is shorter than the video, "
            "the start is automatically shifted earlier so the song covers the full video."
        ),
    )
    parser.add_argument(
        "--music", metavar="URL_OR_QUERY", default=None,
        help=(
            "Add music to the combined video. Provide a YouTube URL (recommended) "
            "or a search query (we'll show 5 results to choose from). "
            "When used, two files are generated: one labelled 'for-instagram' (no music) "
            "and one 'with-music' for personal use. "
            "WARNING: uploading the music version to Instagram may trigger copyright detection."
        ),
    )
    parser.add_argument(
        "--no-replay", action="store_true", default=False,
        help=(
            "Use this ONLY if the competition video has no slow-motion replays after each lift. "
            "Most federation broadcasts (AEP, IPF…) include a replay, so the default is replay=on. "
            "When set, all clip durations are halved automatically. "
            "Recommended: leave this unset unless you are sure there are no replays."
        ),
    )

    # ── Single lift mode ──────────────────────────────────────────────────────
    single = parser.add_argument_group(
        "single lift mode",
        "Extract one lift only. Use --single with --timestamp (and --movement / --attempt).",
    )
    single.add_argument(
        "--single", action="store_true", default=False,
        help="Enable single-lift mode (requires --timestamp)",
    )
    single.add_argument(
        "--timestamp", metavar="TS", default=None,
        help="Timestamp of the lift (e.g. '2h26:15' or '2:26:15'). Required with --single.",
    )
    single.add_argument(
        "--movement", choices=["squat", "bench", "deadlift"], default="squat",
        help="Movement type (default: squat)",
    )
    single.add_argument(
        "--attempt", type=int, choices=[1, 2, 3], default=3,
        help="Attempt number, used only in the output filename (default: 3)",
    )
    single.add_argument(
        "--audio-mode", dest="audio_mode",
        choices=["original", "music_only", "mixed"], default="original",
        help=(
            "original: keep original audio (1 file, no copyright risk). "
            "music_only: replace audio with music (requires --music). "
            "mixed: blend original + music, 3 files generated (requires --music)."
        ),
    )
    single.add_argument(
        "--mix-level", dest="mix_pct", type=int, default=40, metavar="N",
        help=(
            "Music percentage in the mixed blend (10-90, default 40). "
            "Original gets (100-N)%%. Only used with --audio-mode mixed."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.url:
        cli_mode(args)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
