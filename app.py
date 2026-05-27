#!/usr/bin/env python3
"""
Flask web interface for the Powerlifting Clip Extractor.

Run with:
    python3 app.py
Then open http://localhost:5000 in your browser.
From another machine on the same network: http://mordor:5000
"""

import contextlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

_FIND_LIFTER = Path(__file__).with_name("find_lifter.py")

from altcha import verify_solution_v1
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from auth import auth_bp, login_manager
from admin import admin_bp
import geoip
import github_issues
from db import (add_feedback, get_feedback_for_user, get_staging_access,
                init_db, record_job_stat, update_feedback_from_github)
from extract_lifts import parse_timestamp, run, run_single
from limiter import limiter

app = Flask(__name__)


@app.template_filter("datetimeformat")
def _datetimeformat(ts: int) -> str:
    import datetime
    d = datetime.datetime.fromtimestamp(ts)
    return f"{d.day:02d}/{d.month:02d} {d.hour:02d}:{d.minute:02d}"


@app.template_filter("fromjson")
def _fromjson(s: str):
    try:
        return json.loads(s)
    except Exception:
        return []


# Persist secret key across restarts so sessions survive server reloads
_key_file = Path("secret.key")
app.secret_key = _key_file.read_bytes() if _key_file.exists() else (
    lambda k: (_key_file.write_bytes(k), k)[1]
)(os.urandom(24))

login_manager.init_app(app)
limiter.init_app(app)
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)

init_db()

if os.environ.get("STAGING"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

    @limiter.request_filter
    def _staging_admin_exempt():
        return current_user.is_authenticated and current_user.is_admin


@app.context_processor
def _inject_staging():
    return {"is_staging": bool(os.environ.get("STAGING"))}


@app.before_request
def _staging_gate():
    if not os.environ.get("STAGING"):
        return
    # Let auth routes and the webhook through unauthenticated
    if request.endpoint and (request.endpoint.startswith("auth.") or
                             request.endpoint == "github_webhook"):
        return
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.is_admin:
        return
    expiry = get_staging_access(int(current_user.get_id()))
    if not expiry or time.time() > expiry:
        return "Staging — acceso no autorizado", 403

EXPIRY_SECONDS = 24 * 3600  # files are kept for 24 hours after the job finishes
MAX_WORKERS = 2

# ── Channel whitelist (statistics / privacy-by-design) ────────────────────────
# Only video URLs from channels in this list are recorded in the jobs log.
# Add the YouTube handle (@name) of any federation that streams public competitions.
# GDPR note: a public competition video URL is not personal data; recording it
# under legitimate interest is fine. Arbitrary user-provided URLs are NOT stored.
YOUTUBE_CHANNEL_WHITELIST: list[str] = [
    "@powerliftingaep4634",      # Asociación Española de Powerlifting
    "@powerliftingtv",           # International Powerlifting Federation
    "@europeanpowerlifting",     # European Powerlifting Federation
    "gbpowerfed",                # British Powerlifting
    "@usapowerlifting1",         # USA Powerlifting (USAPL)
    "@canadapowerlifting",       # Canadian Powerlifting Union (CPU)
]


def _resolve_channel_url(video_url: str) -> str:
    """Return the YouTube channel URL for a video, or '' on any failure.

    Uses yt-dlp --skip-download so no video is fetched; takes ~1–2 s.
    Should only be called for real jobs (not dry-run).
    """
    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--print", "uploader_url",
             "--quiet", "--no-playlist", video_url],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _channel_whitelisted(channel_url: str) -> bool:
    """Return True if channel_url contains any whitelisted handle."""
    low = channel_url.lower()
    return any(handle.lower() in low for handle in YOUTUBE_CHANNEL_WHITELIST)


# ── Competition selector ───────────────────────────────────────────────────────

_SOURCES: dict[str, str] = {
    "aep":  "https://www.youtube.com/@powerliftingaep4634/playlists",
    "epf":  "https://www.youtube.com/@europeanpowerlifting/playlists",
    "ipf":  "https://www.youtube.com/@powerliftingtv/playlists",
    "usapl": "https://www.youtube.com/@USAPowerlifting1/playlists",
    "bp":   "https://www.youtube.com/@gbpowerfed-britishpowerlifting/playlists",
    "cpu":  "https://www.youtube.com/@canadapowerlifting/playlists",
}

# (timestamp, grouped_data)
_channel_cache: dict[str, tuple[float, dict]] = {}
_playlist_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 6 * 3600  # 6 hours

import re as _re

_TITLE_NOISE = _re.compile(
    r"^[\U00010000-\U0010ffff☀-➿︀-️‍\s]*"  # leading emoji/spaces
    r"|🔴\s*LIVE\s*:\s*"
    r"|LIVE\s*:\s*",
    _re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    title = _TITLE_NOISE.sub("", raw).strip()
    return title[:95] + "…" if len(title) > 95 else title


def _fetch_channel_playlists(source: str) -> dict:
    """Fetch channel /playlists page and return competitions grouped by year.

    Each entry is a playlist (one competition). Year is extracted from the
    title since /playlists metadata never includes upload_date/timestamp.
    Returns {year: [{title, url}, ...]} sorted newest-first.
    """
    channel_url = _SOURCES[source]
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-j", "--quiet",
         "--playlist-end", "200", channel_url],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed")

    by_year: dict[str, list[dict]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_title = entry.get("title") or ""
        if not raw_title or raw_title in ("[Deleted video]", "[Private video]"):
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url:
            continue
        # Only keep playlist URLs (competitions), not stray video URLs
        if "playlist?list=" not in url and "/playlist/" not in url:
            continue

        title = _clean_title(raw_title)
        m = _re.search(r"\b(20\d{2})\b", raw_title)
        year = m.group(1) if m else "?"
        by_year.setdefault(year, []).append({"title": title, "url": url})

    return dict(sorted(by_year.items(), reverse=True))


def _fetch_playlist_sessions(playlist_url: str) -> list:
    """Expand a competition playlist into individual session videos.

    Returns [{title, url}, ...] filtering out deleted/private entries.
    """
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-j", "--quiet", playlist_url],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed")

    sessions = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_title = entry.get("title") or ""
        if not raw_title or raw_title in ("[Deleted video]", "[Private video]"):
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url:
            continue
        title = _clean_title(raw_title)
        duration = entry.get("duration")  # seconds, may be None
        sessions.append({"title": title, "url": url, "duration": duration})
    return sessions


@app.route("/api/channel-videos")
@login_required
def channel_videos():
    source = request.args.get("source", "")
    if source not in _SOURCES:
        return jsonify({"error": "Unknown source"}), 400

    now = time.time()
    cached = _channel_cache.get(source)
    if cached and now - cached[0] < _CACHE_TTL:
        return jsonify(cached[1])

    try:
        data = _fetch_channel_playlists(source)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    _channel_cache[source] = (now, data)
    return jsonify(data)


@app.route("/api/playlist-sessions")
@login_required
def playlist_sessions():
    url = request.args.get("url", "")
    if not url or "youtube.com/playlist" not in url:
        return jsonify({"error": "Invalid playlist URL"}), 400

    now = time.time()
    cached = _playlist_cache.get(url)
    if cached and now - cached[0] < _CACHE_TTL:
        return jsonify(cached[1])

    try:
        sessions = _fetch_playlist_sessions(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    _playlist_cache[url] = (now, sessions)
    return jsonify(sessions)

@app.route("/api/channel-search")
@login_required
def channel_search():
    source = request.args.get("source", "")
    q = request.args.get("q", "").strip()
    if source not in _SOURCES:
        return jsonify({"error": "Unknown source"}), 400
    if not q:
        return jsonify([])

    channel_base = _SOURCES[source].replace("/playlists", "")
    search_url = f"{channel_base}/search?query={urllib.parse.quote(q)}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "-j", "--quiet", search_url],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return jsonify([])

    # Words from query used to filter YouTube's off-topic results
    query_words = [w.lower() for w in q.split() if len(w) >= 3]

    entries = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_title = entry.get("title") or ""
        if not raw_title or raw_title in ("[Deleted video]", "[Private video]"):
            continue
        # Drop results where none of the query words appear in the title
        title_lower = raw_title.lower()
        if query_words and not any(w in title_lower for w in query_words):
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url:
            continue
        entries.append({
            "title":    _clean_title(raw_title),
            "url":      url,
            "duration": entry.get("duration"),
        })
    return jsonify(entries)


# In-memory cache — populated from disk on cache miss so server restarts are safe
jobs: dict[str, dict] = {}

# FIFO job queue consumed by a fixed pool of worker threads
_job_queue: queue.Queue = queue.Queue()


# ── Persistence ────────────────────────────────────────────────────────────────

def _status_path(job_id: str) -> Path:
    return Path("lifts") / job_id[:8] / "status.json"


def _save_job(job_id: str, job: dict) -> None:
    path = _status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status":        job["status"],
        "log":           job["log"],
        "output_dir":    job["output_dir"],
        "expires_at":    job.get("expires_at"),
        "mode":          job.get("mode", "full"),
        "job_id":        job_id,
        "user_id":       job.get("user_id"),
        "submitted_url": job.get("submitted_url"),
        "source":        job.get("source"),
        "session_label": job.get("session_label"),
        "queued_at":     job.get("queued_at"),
        "ocr_result":    job.get("ocr_result"),
        "ocr_apellido":  job.get("ocr_apellido"),
    }))


def _load_job(job_id: str) -> dict | None:
    path = _status_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ── Expiry / cleanup ───────────────────────────────────────────────────────────

def _cleanup_loop() -> None:
    """Background thread: delete job directories whose expiry has passed."""
    while True:
        time.sleep(3600)
        now = time.time()
        lifts_root = Path("lifts")
        if not lifts_root.exists():
            continue
        for job_dir in lifts_root.iterdir():
            if not job_dir.is_dir():
                continue
            status_file = job_dir / "status.json"
            if not status_file.exists():
                continue  # CLI-generated directory — never auto-delete
            try:
                data = json.loads(status_file.read_text())
                expires_at = data.get("expires_at")
                if expires_at and now > expires_at:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    # Evict from memory cache
                    for jid, job in list(jobs.items()):
                        if job.get("output_dir") == str(job_dir):
                            jobs.pop(jid, None)
            except Exception:
                pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


# ── Queue / worker pool ────────────────────────────────────────────────────────

def _queue_worker() -> None:
    """Persistent worker thread: pulls jobs from the queue and executes them."""
    while True:
        job_id, run_kwargs, mode = _job_queue.get()
        job = jobs.get(job_id)
        if job is not None:
            job["status"] = "running"
            _save_job(job_id, job)
            _worker(job_id, job, run_kwargs, mode)
        _job_queue.task_done()


def _queue_position(job_id: str) -> int:
    """Return the 1-based position in the pending queue, or 0 if not queued."""
    job = jobs.get(job_id)
    if not job or job["status"] != "queued":
        return 0
    target_t = job.get("queued_at", 0.0)
    ahead = sum(
        1 for jid, j in jobs.items()
        if jid != job_id and j["status"] == "queued"
        and j.get("queued_at", 0.0) < target_t
    )
    return ahead + 1


for _ in range(MAX_WORKERS):
    threading.Thread(target=_queue_worker, daemon=True).start()


# ── Live log buffer ────────────────────────────────────────────────────────────

class _LiveLog(io.StringIO):
    """StringIO that updates job["log"] on every write and flushes to disk every 5 s."""
    def __init__(self, job_id: str, job: dict):
        super().__init__()
        self._job_id = job_id
        self._job = job
        self._last_flush = 0.0

    def write(self, s: str) -> int:
        result = super().write(s)
        self._job["log"] = self.getvalue()
        now = time.monotonic()
        if now - self._last_flush >= 5.0:
            _save_job(self._job_id, self._job)
            self._last_flush = now
        return result


# ── Worker ─────────────────────────────────────────────────────────────────────

def _ocr_worker(job_id: str, job: dict) -> None:
    """Execute find_lifter.py as a subprocess, stream stderr to live log."""
    buf = _LiveLog(job_id, job)
    started_at = time.time()
    url = job["submitted_url"]
    apellido = job.get("ocr_apellido", "")
    work_dir = Path(job["output_dir"]) / "ocr"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_FIND_LIFTER), url, apellido, "--work-dir", str(work_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def _drain_stderr() -> None:
            for line in proc.stderr:
                buf.write(line)

        drain_t = threading.Thread(target=_drain_stderr, daemon=True)
        drain_t.start()
        stdout, _ = proc.communicate()
        drain_t.join(timeout=5)

        if proc.returncode != 0:
            raise RuntimeError(f"find_lifter.py falló (exit {proc.returncode})")
        result = json.loads(stdout.strip())
        job["ocr_result"] = result
        job["status"] = "ocr_done"
        job["expires_at"] = time.time() + EXPIRY_SECONDS
    except Exception as e:
        job["status"] = "error"
        buf.write(f"\nError: {e}\n")
    finally:
        job["log"] = buf.getvalue()
        _save_job(job_id, job)
        geo = job.get("_geo") or {}
        record_job_stat(
            submitted_at=job.get("queued_at", started_at),
            started_at=started_at,
            finished_at=time.time(),
            status=job["status"],
            source=job.get("source") or None,
            mode="ocr",
            has_music=0,
            city=geo.get("city"),
            country_code=geo.get("country_code"),
            latitude=geo.get("lat"),
            longitude=geo.get("lng"),
        )


def _worker(job_id: str, job: dict, run_kwargs: dict, mode: str = "full") -> None:
    if mode == "ocr":
        _ocr_worker(job_id, job)
        return

    buf = _LiveLog(job_id, job)
    started_at = time.time()
    try:
        with contextlib.redirect_stdout(buf):
            if mode == "single":
                run_single(**run_kwargs)
            else:
                run(**run_kwargs)
        job["status"] = "done"
        job["expires_at"] = time.time() + EXPIRY_SECONDS
        # Resolve channel for stats logging — only for real (non-dry-run) jobs
        if not run_kwargs.get("dry_run"):
            channel_url = _resolve_channel_url(run_kwargs["url"])
            if _channel_whitelisted(channel_url):
                job["video_url"] = run_kwargs["url"]
                job["channel_url"] = channel_url
    except SystemExit as e:
        job["status"] = "error"
        buf.write(f"\nFailed: {e}\n")
    except Exception as e:
        job["status"] = "error"
        buf.write(f"\nUnexpected error: {e}\n")
    finally:
        job["log"] = buf.getvalue()
        _save_job(job_id, job)
        geo = job.get("_geo") or {}
        record_job_stat(
            submitted_at=job.get("queued_at", started_at),
            started_at=started_at,
            finished_at=time.time(),
            status=job["status"],
            source=job.get("source") or None,
            mode=job.get("mode") or None,
            has_music=1 if run_kwargs.get("music_source") else 0,
            city=geo.get("city"),
            country_code=geo.get("country_code"),
            latitude=geo.get("lat"),
            longitude=geo.get("lng"),
        )


# ── Form parsing ───────────────────────────────────────────────────────────────

class FormError(ValueError):
    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


def _build_single_run_kwargs(form, url: str, output_dir: Path) -> dict:
    ts_raw = form.get("single_timestamp", "").strip()
    if not ts_raw:
        raise FormError("El timestamp es obligatorio.", field="single_timestamp")
    try:
        timestamp = parse_timestamp(ts_raw)
    except ValueError as e:
        raise FormError(str(e), field="single_timestamp")

    movement = form.get("single_movement", "squat")
    if movement not in ("squat", "bench", "deadlift"):
        raise FormError("Movimiento no válido.", field="single_movement")

    try:
        attempt = int(form.get("single_attempt", "3"))
        if attempt not in (1, 2, 3):
            raise ValueError
    except ValueError:
        raise FormError("Intento no válido.", field="single_attempt")

    audio_mode = form.get("audio_mode", "original")
    if audio_mode not in ("original", "music_only", "mixed"):
        raise FormError("Modo de audio no válido.", field="audio_mode")

    music_source = form.get("music", "").strip()
    if audio_mode in ("music_only", "mixed") and not music_source:
        raise FormError("Se requiere una canción para este modo de audio.", field="music")

    music_start_raw = form.get("music_start", "").strip()
    music_start = float(parse_timestamp(music_start_raw)) if music_start_raw else 0.0

    music_pct = 50
    if "mix_custom" in form:
        try:
            music_pct = max(10, min(90, int(form.get("music_pct") or 50)))
        except ValueError:
            pass

    preview_width = 0
    if "preview" in form:
        preview_width = int(form.get("preview_width") or 640)

    return dict(
        _mode="single",
        url=url,
        timestamp=timestamp,
        movement=movement,
        attempt=attempt,
        duration=int(form.get("duration") or 60),
        output_dir=output_dir,
        audio_mode=audio_mode,
        music_source=music_source,
        music_start=music_start,
        music_pct=music_pct,
        preview_width=preview_width,
        no_replay="no_replay" in form,
        dry_run="dry_run" in form,
        interactive=False,
    )


def _build_run_kwargs(form, files, output_dir: Path) -> dict:
    url = form.get("url", "").strip()
    if not url:
        raise FormError("YouTube URL is required.", field="url")

    if form.get("single_lift"):
        return _build_single_run_kwargs(form, url, output_dir)

    if form.get("timestamps_mode") == "file":
        uploaded = files.get("timestamps_file")
        if not uploaded or uploaded.filename == "":
            raise FormError("Please select a timestamps file.", field="timestamps_file")
        content = uploaded.read().decode("utf-8")
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if len(lines) != 9:
            raise FormError(f"Expected 9 timestamps, got {len(lines)}.", field="timestamps_file")
        timestamps = [parse_timestamp(l) for l in lines]
    else:
        lines = [l.strip() for l in form.get("timestamps_text", "").splitlines() if l.strip()]
        if len(lines) != 9:
            raise FormError(f"Expected 9 timestamps, got {len(lines)}.", field="timestamps_text")
        timestamps = [parse_timestamp(l) for l in lines]

    duration = int(form.get("duration") or 60)
    durations = {
        "squat":    int(form.get("duration_squat")    or 0) or duration,
        "bench":    int(form.get("duration_bench")    or 0) or duration,
        "deadlift": int(form.get("duration_deadlift") or 0) or duration,
    }

    preview_width = 0
    if "preview" in form:
        preview_width = int(form.get("preview_width") or 640)

    music_source = form.get("music", "").strip()
    music_start_raw = form.get("music_start", "").strip()
    music_start = float(parse_timestamp(music_start_raw)) if music_start_raw else 0.0

    clip_dur_vals = [form.get(f"clip_duration_{i}", "").strip() for i in range(9)]
    clip_durations = None
    if all(clip_dur_vals):
        try:
            clip_durations = [max(10, int(v)) for v in clip_dur_vals]
        except ValueError:
            clip_durations = None

    return dict(
        url=url,
        timestamps=timestamps,
        durations=durations,
        clip_durations=clip_durations,
        squat_attempt=int(form.get("squat_attempt", 3)),
        bench_attempt=int(form.get("bench_attempt", 3)),
        deadlift_attempt=int(form.get("deadlift_attempt", 3)),
        output_dir=output_dir,
        skip_individual="skip_individual" in form,
        skip_combined="skip_combined" in form,
        preview_width=preview_width,
        no_replay="no_replay" in form,
        music_source=music_source,
        music_start=music_start,
        interactive=False,
        dry_run="dry_run" in form,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.errorhandler(429)
def ratelimit_handler(e):
    description = str(e.description)
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify(error="Demasiadas peticiones. Espera un momento.", detail=description), 429
    return render_template("429.html", detail=description), 429


@app.route("/run", methods=["POST"])
@login_required
@limiter.limit("1 per 2 minutes", key_func=lambda: current_user.get_id())
def start_job():
    job_id = uuid.uuid4().hex
    output_dir = Path("lifts") / job_id[:8]
    try:
        run_kwargs = _build_run_kwargs(request.form, request.files, output_dir)
    except FormError as e:
        return render_template("index.html",
                               error=str(e),
                               error_field=e.field,
                               form_data=request.form), 400

    mode = run_kwargs.pop("_mode", "full")
    job = {"status": "queued", "log": "", "output_dir": str(output_dir),
           "expires_at": None, "queued_at": time.time(), "mode": mode,
           "user_id": current_user.get_id(),
           "submitted_url": request.form.get("url", "").strip(),
           "source": request.form.get("source", "").strip(),
           "session_label": request.form.get("session_label", "").strip(),
           "_geo": geoip.lookup(request.headers.get("X-Real-IP", request.remote_addr))}
    jobs[job_id] = job
    _save_job(job_id, job)
    _job_queue.put((job_id, run_kwargs, mode))
    return redirect(url_for("status", job_id=job_id))


@app.route("/run-ocr", methods=["POST"])
@login_required
@limiter.limit("1 per 30 minutes", key_func=lambda: current_user.get_id())
def start_ocr_job():
    url = request.form.get("url", "").strip()
    apellido = request.form.get("ocr_apellido", "").strip()[:60]

    if not url:
        return render_template("index.html", error="URL de YouTube obligatoria.",
                               error_field="url", form_data=request.form), 400
    if not apellido:
        return render_template("index.html", error="El apellido es obligatorio.",
                               error_field="ocr_apellido", form_data=request.form), 400

    job_id = uuid.uuid4().hex
    output_dir = Path("lifts") / job_id[:8]
    job = {
        "status": "queued", "log": "", "output_dir": str(output_dir),
        "expires_at": None, "queued_at": time.time(), "mode": "ocr",
        "user_id": current_user.get_id(),
        "submitted_url": url,
        "ocr_apellido": apellido,
        "source": request.form.get("source", "").strip(),
        "session_label": request.form.get("session_label", "").strip(),
        "_geo": geoip.lookup(request.headers.get("X-Real-IP", request.remote_addr)),
        "ocr_result": None,
    }
    jobs[job_id] = job
    _save_job(job_id, job)
    _job_queue.put((job_id, {}, "ocr"))
    return redirect(url_for("status", job_id=job_id))


@app.route("/ocr/<job_id>/review")
@login_required
def ocr_review(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job or job.get("status") != "ocr_done" or not job.get("ocr_result"):
        abort(404)
    return render_template("ocr_review.html", job_id=job_id, job=job,
                           ocr_result=job["ocr_result"])


@app.route("/status/<job_id>")
@login_required
def status(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    single_mode = job.get("mode") == "single" if job else False
    ocr_mode = job.get("mode") == "ocr" if job else False
    return render_template("status.html", job_id=job_id,
                           single_mode=single_mode, ocr_mode=ocr_mode)


@app.route("/status/<job_id>/json")
@login_required
def status_json(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    jobs[job_id] = job

    resp = {
        "status":     job["status"],
        "log":        job["log"],
        "expires_at": job.get("expires_at"),
        "queue_pos":  _queue_position(job_id),
    }
    if job.get("ocr_result") is not None:
        resp["ocr_result"] = job["ocr_result"]
    return jsonify(resp)


@app.route("/download/<job_id>/zip")
@login_required
def download_zip(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404

    expires_at = job.get("expires_at")
    if expires_at and time.time() > expires_at:
        return render_template("expired.html"), 410

    out_dir = Path(job["output_dir"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out_dir.rglob("*.mp4")):
            zf.write(f, f.relative_to(out_dir))
    buf.seek(0)
    safe_name = current_user.display_name.replace(" ", "_")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name}.zip")


def _make_issue_title(subject: str, body: str) -> str:
    if subject.strip():
        return subject.strip()
    text = body.strip()
    if len(text) <= 60:
        return text
    return text[:60].rsplit(" ", 1)[0] + "…"


@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    user_id = int(current_user.get_id())

    if request.method == "POST":
        body_text = request.form.get("body", "").strip()
        subject = request.form.get("subject", "").strip()[:80]

        error = None
        if not body_text:
            error = "fb.error.empty"
        elif len(body_text) > 2000:
            error = "fb.error.toolong"

        if error is None and not app.config.get("TESTING"):
            key = app.secret_key
            altcha_key = key.hex() if isinstance(key, bytes) else key
            ok, _ = verify_solution_v1(request.form.get("altcha", ""), altcha_key,
                                       check_expires=False)
            if not ok:
                feedbacks = get_feedback_for_user(user_id)
                return render_template("feedback.html", feedbacks=feedbacks,
                                       error_captcha=True), 400

        if error:
            feedbacks = get_feedback_for_user(user_id)
            return render_template("feedback.html", feedbacks=feedbacks,
                                   error=error), 400

        title = _make_issue_title(subject, body_text)
        try:
            issue = github_issues.create_issue(title, body_text)
        except Exception:
            feedbacks = get_feedback_for_user(user_id)
            return render_template("feedback.html", feedbacks=feedbacks,
                                   error="fb.error.github"), 502

        excerpt = body_text[:100] + ("…" if len(body_text) > 100 else "")
        add_feedback(user_id, issue["number"], issue["html_url"], title, excerpt)
        return redirect(url_for("feedback"))

    feedbacks = get_feedback_for_user(user_id)
    return render_template("feedback.html", feedbacks=feedbacks)


@app.route("/webhook/github", methods=["POST"])
def github_webhook():
    body = request.get_data()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not github_issues.verify_signature(body, sig):
        abort(400)

    if request.headers.get("X-GitHub-Event") != "issues":
        return "", 204

    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "")
    issue = payload.get("issue", {})
    issue_number = issue.get("number")

    if issue_number and action in ("closed", "reopened", "labeled", "unlabeled"):
        if action == "closed":
            state_reason = issue.get("state_reason") or "completed"
            if state_reason == "not_planned":
                status = "wontfix"
            elif state_reason == "duplicate":
                status = "duplicate"
            else:
                status = "closed"
        else:
            status = "open"
        labels = [lbl["name"] for lbl in issue.get("labels", [])]
        update_feedback_from_github(issue_number, status, json.dumps(labels))

    return "", 204


@app.route("/history")
@login_required
def history():
    lifts_root = Path("lifts")
    now = time.time()
    my_jobs = []
    if lifts_root.exists():
        for job_dir in lifts_root.iterdir():
            if not job_dir.is_dir():
                continue
            status_file = job_dir / "status.json"
            if not status_file.exists():
                continue
            try:
                data = json.loads(status_file.read_text())
            except Exception:
                continue
            if str(data.get("user_id")) != current_user.get_id():
                continue
            data["expired"] = bool(data.get("expires_at") and now > data["expires_at"])
            my_jobs.append(data)
    my_jobs.sort(key=lambda d: d.get("queued_at") or 0, reverse=True)
    return render_template("history.html", jobs=my_jobs[:20])


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
