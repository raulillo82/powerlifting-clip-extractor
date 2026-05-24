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
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from auth import auth_bp, login_manager
from admin import admin_bp
from db import init_db
from extract_lifts import parse_timestamp, run, run_single
from limiter import limiter

app = Flask(__name__)

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

EXPIRY_SECONDS = 24 * 3600  # files are kept for 24 hours after the job finishes
MAX_WORKERS = 2

# ── Channel whitelist (statistics / privacy-by-design) ────────────────────────
# Only video URLs from channels in this list are recorded in the jobs log.
# Add the YouTube handle (@name) of any federation that streams public competitions.
# GDPR note: a public competition video URL is not personal data; recording it
# under legitimate interest is fine. Arbitrary user-provided URLs are NOT stored.
YOUTUBE_CHANNEL_WHITELIST: list[str] = [
    "@AEpowerlifting",       # Asociación Española de Powerlifting
    "@IPF_Powerlifting",     # International Powerlifting Federation
    "@EPF_Powerlifting",     # European Powerlifting Federation
    "@BritishPowerlifting",  # British Powerlifting
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
        "status":     job["status"],
        "log":        job["log"],
        "output_dir": job["output_dir"],
        "expires_at": job.get("expires_at"),
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

def _worker(job_id: str, job: dict, run_kwargs: dict, mode: str = "full") -> None:
    buf = _LiveLog(job_id, job)
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

    return dict(
        url=url,
        timestamps=timestamps,
        durations=durations,
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
           "expires_at": None, "queued_at": time.time()}
    jobs[job_id] = job
    _save_job(job_id, job)
    _job_queue.put((job_id, run_kwargs, mode))
    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
@login_required
def status(job_id: str):
    return render_template("status.html", job_id=job_id)


@app.route("/status/<job_id>/json")
@login_required
def status_json(job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    jobs[job_id] = job

    return jsonify({
        "status":     job["status"],
        "log":        job["log"],
        "expires_at": job.get("expires_at"),
        "queue_pos":  _queue_position(job_id),
    })


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
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="powerlifting_clips.zip")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
