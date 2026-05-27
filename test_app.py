"""Flask route tests using the built-in test client."""
import io
import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

import app as flask_app
import db

# ── Helpers ────────────────────────────────────────────────────────────────────

VALID_TIMESTAMPS = "\n".join([
    "0:21:27", "0:29:55", "0:38:15",
    "1h23:30", "1h32:21", "1h41:30",
    "2h26:15", "2h33:4",  "2h41:35",
])

VALID_FORM = {
    "url":             "https://www.youtube.com/watch?v=test",
    "timestamps_mode": "paste",
    "timestamps_text": VALID_TIMESTAMPS,
    "duration":        "60",
    "squat_attempt":   "3",
    "bench_attempt":   "3",
    "deadlift_attempt":"3",
}


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_jobs():
    flask_app.jobs.clear()
    yield
    flask_app.jobs.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Redirect DB to a temp file so tests never touch users.db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    # Create a test admin user and log in
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin)"
            " VALUES (?, ?, ?, 1)",
            ("testadmin", "Test Admin", generate_password_hash("testpass")),
        )
        conn.commit()

    import limiter as limiter_mod
    flask_app.app.config["TESTING"] = True
    monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
    with flask_app.app.test_client() as c:
        c.post("/login", data={"username": "testadmin", "password": "testpass"})
        yield c


def _reset_limiter():
    import limiter as limiter_mod
    limiter_mod.limiter.reset()


@pytest.fixture
def client_limited(tmp_path, monkeypatch):
    """Authenticated client with rate limiting active."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin)"
            " VALUES (?, ?, ?, 1)",
            ("testadmin", "Test Admin", generate_password_hash("testpass")),
        )
        conn.commit()
    flask_app.app.config["TESTING"] = True
    _reset_limiter()
    with flask_app.app.test_client() as c:
        c.post("/login", data={"username": "testadmin", "password": "testpass"})
        yield c


@pytest.fixture
def anon_client_limited(tmp_path, monkeypatch):
    """Unauthenticated client with rate limiting active."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    flask_app.app.config["TESTING"] = True
    _reset_limiter()
    with flask_app.app.test_client() as c:
        yield c


# ── Index ──────────────────────────────────────────────────────────────────────

class TestIndex:
    def test_returns_200(self, client):
        assert client.get("/").status_code == 200

    def test_contains_form(self, client):
        assert b"<form" in client.get("/").data


# ── /run ──────────────────────────────────────────────────────────────────────

class TestRun:
    def _post(self, client, overrides=None):
        data = {**VALID_FORM, **(overrides or {})}
        with patch("app._save_job"), patch("app._job_queue.put"):
            return client.post("/run", data=data, follow_redirects=False)

    def test_missing_url_returns_400(self, client):
        assert self._post(client, {"url": ""}).status_code == 400

    def test_missing_url_highlights_field(self, client):
        r = self._post(client, {"url": ""})
        assert b'id="url"' in r.data
        assert b"is-invalid" in r.data

    def test_missing_url_repopulates_timestamps(self, client):
        r = self._post(client, {"url": ""})
        assert VALID_TIMESTAMPS[:10].encode() in r.data

    def test_too_few_timestamps_returns_400(self, client):
        assert self._post(client, {"timestamps_text": "0:21:27\n0:29:55"}).status_code == 400

    def test_too_few_timestamps_highlights_field(self, client):
        r = self._post(client, {"timestamps_text": "0:21:27\n0:29:55"})
        assert b"is-invalid" in r.data

    def test_valid_form_redirects_to_status(self, client):
        r = self._post(client)
        assert r.status_code == 302
        assert "/status/" in r.headers["Location"]

    def test_valid_form_creates_queued_job(self, client):
        self._post(client)
        assert len(flask_app.jobs) == 1
        assert next(iter(flask_app.jobs.values()))["status"] == "queued"

    def test_file_mode_missing_file_returns_400(self, client):
        assert self._post(client, {"timestamps_mode": "file"}).status_code == 400

    def test_file_mode_wrong_count_returns_400(self, client):
        data = {**VALID_FORM, "timestamps_mode": "file",
                "timestamps_file": (io.BytesIO(b"0:21:27\n0:29:55"), "times.txt")}
        with patch("app._save_job"), patch("app.threading.Thread"):
            r = client.post("/run", data=data, follow_redirects=False)
        assert r.status_code == 400

    def test_file_mode_valid_file_redirects(self, client):
        data = {**VALID_FORM, "timestamps_mode": "file",
                "timestamps_file": (io.BytesIO(VALID_TIMESTAMPS.encode()), "times.txt")}
        with patch("app._save_job"), patch("app.threading.Thread"):
            r = client.post("/run", data=data, follow_redirects=False)
        assert r.status_code == 302


# ── /status/<id> ──────────────────────────────────────────────────────────────

class TestStatusPage:
    def test_any_job_id_returns_200(self, client):
        assert client.get("/status/anyjobid").status_code == 200


# ── /status/<id>/json ─────────────────────────────────────────────────────────

class TestStatusJson:
    def test_known_job_returns_status(self, client):
        flask_app.jobs["abc"] = {"status": "running", "log": "", "expires_at": None}
        r = client.get("/status/abc/json")
        assert r.status_code == 200
        assert json.loads(r.data)["status"] == "running"

    def test_unknown_job_returns_404(self, client):
        with patch("app._load_job", return_value=None):
            r = client.get("/status/doesnotexist/json")
        assert r.status_code == 404

    def test_disk_fallback_on_cache_miss(self, client):
        on_disk = {"status": "done", "log": "ok",
                   "output_dir": "lifts/abc12345", "expires_at": None}
        with patch("app._load_job", return_value=on_disk):
            r = client.get("/status/abc12345/json")
        assert r.status_code == 200
        assert json.loads(r.data)["status"] == "done"


# ── /download/<id>/zip ────────────────────────────────────────────────────────

class TestDownloadZip:
    def test_unknown_job_returns_404(self, client):
        with patch("app._load_job", return_value=None):
            assert client.get("/download/nosuchjob/zip").status_code == 404

    def test_running_job_returns_404(self, client):
        flask_app.jobs["runjob"] = {"status": "running", "log": "",
                                    "output_dir": "x", "expires_at": None}
        assert client.get("/download/runjob/zip").status_code == 404

    def test_expired_job_returns_410(self, client):
        flask_app.jobs["expjob"] = {"status": "done", "log": "",
                                    "output_dir": "lifts/expjob",
                                    "expires_at": time.time() - 1}
        assert client.get("/download/expjob/zip").status_code == 410


# ── /login ────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_login_page_returns_200(self, client):
        # Log out first so we can see the login page
        client.get("/logout")
        assert client.get("/login").status_code == 200

    def test_login_with_valid_credentials(self, client, tmp_path, monkeypatch):
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test2.db")
        db_mod.init_db()
        with db_mod.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (?,?,?)",
                ("heather_connor", "Heather Connor", generate_password_hash("pass")),
            )
            conn.commit()
        with flask_app.app.test_client() as c:
            r = c.post("/login", data={"username": "heather_connor", "password": "pass"},
                       follow_redirects=False)
        assert r.status_code == 302

    def test_wrong_password_shows_error(self, client):
        client.get("/logout")
        r = client.post("/login", data={"username": "testadmin", "password": "wrong"})
        assert r.status_code == 200
        assert b"incorrectos" in r.data

    def test_inactive_user_shows_error(self, client):
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_active)"
                " VALUES (?,?,?,0)",
                ("russel_orhii", "Russel Orhii", generate_password_hash("pass")),
            )
            conn.commit()
        client.get("/logout")
        r = client.post("/login", data={"username": "russel_orhii", "password": "pass"})
        assert b"desactivada" in r.data

    def test_wrong_device_shows_error(self, client):
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, device_token)"
                " VALUES (?,?,?,?)",
                ("agata_sitko", "Agata Sitko", generate_password_hash("pass"), "device-XYZ"),
            )
            conn.commit()
        client.get("/logout")
        # Client's device_token cookie doesn't match "device-XYZ"
        r = client.post("/login", data={"username": "agata_sitko", "password": "pass"})
        assert b"vinculada" in r.data

    def test_logout_redirects_to_login(self, client):
        r = client.get("/logout", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_unauthenticated_access_redirects(self, client):
        client.get("/logout")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ── /register ─────────────────────────────────────────────────────────────────

class TestRegister:
    def test_register_page_shows_slots(self, client):
        # client has a device_token cookie from logging in as testadmin;
        # use a fresh client with no cookies so /register shows the slots form
        with flask_app.app.test_client() as fresh:
            r = fresh.get("/register")
        assert r.status_code == 200
        assert b"50" in r.data  # MAX_USERS slots shown

    def test_register_creates_user(self, client, tmp_path, monkeypatch):
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "reg.db")
        db_mod.init_db()
        with flask_app.app.test_client() as c:
            r = c.post("/register", follow_redirects=False)
        assert r.status_code == 200
        with db_mod.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin=0"
            ).fetchone()[0]
        assert count == 1

    def test_register_same_device_blocked(self, client, tmp_path, monkeypatch):
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "reg2.db")
        db_mod.init_db()
        with flask_app.app.test_client() as c:
            c.post("/register")           # first registration sets cookie
            r = c.get("/register")        # same device visits again
        assert b"already_registered" not in r.data  # Jinja variable not leaked
        # The page should show the "already registered" message
        assert r.status_code == 200


# ── /admin ────────────────────────────────────────────────────────────────────

class TestAdmin:
    def _make_user(self, conn, username="taylor_atwood"):
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, device_token)"
            " VALUES (?,?,?,?)",
            (username, username.replace("_"," ").title(),
             generate_password_hash("x"), "tok"),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)
        ).fetchone()["id"]

    def test_admin_page_lists_users(self, client):
        with db.get_db() as conn:
            self._make_user(conn)
        r = client.get("/admin/")
        assert r.status_code == 200
        assert b"taylor_atwood" in r.data

    def test_non_admin_cannot_access_admin(self, client):
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (?,?,?)",
                ("gustave_hedlund", "Gustav Hedlund", generate_password_hash("p")),
            )
            conn.commit()
        client.get("/logout")
        client.post("/login", data={"username": "gustave_hedlund", "password": "p"})
        r = client.get("/admin/", follow_redirects=False)
        assert r.status_code == 302

    def test_admin_toggle_deactivates_user(self, client):
        with db.get_db() as conn:
            uid = self._make_user(conn, "jessica_buettner")
        client.post(f"/admin/toggle/{uid}")
        with db.get_db() as conn:
            is_active = conn.execute(
                "SELECT is_active FROM users WHERE id=?", (uid,)
            ).fetchone()["is_active"]
        assert is_active == 0

    def test_admin_reset_device(self, client):
        with db.get_db() as conn:
            uid = self._make_user(conn, "heather_connor")
        client.post(f"/admin/reset-device/{uid}")
        with db.get_db() as conn:
            token = conn.execute(
                "SELECT device_token FROM users WHERE id=?", (uid,)
            ).fetchone()["device_token"]
        assert token is None

    def test_admin_delete_user(self, client):
        with db.get_db() as conn:
            uid = self._make_user(conn, "amanda_lawrence")
        client.post(f"/admin/delete/{uid}")
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE id=?", (uid,)
            ).fetchone()
        assert row is None


# ── Dry-run end-to-end ────────────────────────────────────────────────────────

class TestDryRun:
    """End-to-end tests using dry_run=True — no yt-dlp or ffmpeg needed."""

    def _submit(self, client):
        data = {**VALID_FORM, "dry_run": "on"}
        return client.post("/run", data=data, follow_redirects=False)

    def _poll(self, client, job_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(f"/status/{job_id}/json")
            if json.loads(r.data)["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        return json.loads(client.get(f"/status/{job_id}/json").data)

    def _job_id(self, r):
        return r.headers["Location"].split("/status/")[1].strip("/")

    def test_dry_run_redirects_to_status(self, client):
        r = self._submit(client)
        assert r.status_code == 302
        assert "/status/" in r.headers["Location"]

    def test_dry_run_job_completes(self, client):
        r = self._submit(client)
        job = self._poll(client, self._job_id(r))
        assert job["status"] == "done"

    def test_dry_run_creates_placeholder_files(self, client):
        r = self._submit(client)
        job_id = self._job_id(r)
        self._poll(client, job_id)
        out_dir = Path(flask_app.jobs[job_id]["output_dir"])
        mp4s = list(out_dir.rglob("*.mp4"))
        # 9 individual clips + 1 combined
        assert len(mp4s) == 10
        shutil.rmtree(out_dir, ignore_errors=True)

    def test_dry_run_zip_download_works(self, client):
        r = self._submit(client)
        job_id = self._job_id(r)
        self._poll(client, job_id)
        zr = client.get(f"/download/{job_id}/zip")
        assert zr.status_code == 200
        assert "zip" in zr.content_type
        shutil.rmtree(Path(flask_app.jobs[job_id]["output_dir"]), ignore_errors=True)

    def test_dry_run_log_contains_dry_run_marker(self, client):
        r = self._submit(client)
        job_id = self._job_id(r)
        job = self._poll(client, job_id)
        assert "[dry run]" in job["log"]
        shutil.rmtree(Path(flask_app.jobs[job_id]["output_dir"]), ignore_errors=True)


# ── Queue system ──────────────────────────────────────────────────────────────

class TestQueue:
    """Tests for the FIFO worker-pool queue."""

    def test_status_json_includes_queue_pos(self, client):
        flask_app.jobs["q1"] = {
            "status": "queued", "log": "", "output_dir": "x",
            "expires_at": None, "queued_at": time.time(),
        }
        r = client.get("/status/q1/json")
        data = json.loads(r.data)
        assert "queue_pos" in data
        assert data["queue_pos"] >= 1

    def test_running_job_has_zero_queue_pos(self, client):
        flask_app.jobs["r1"] = {
            "status": "running", "log": "", "output_dir": "x",
            "expires_at": None,
        }
        data = json.loads(client.get("/status/r1/json").data)
        assert data["queue_pos"] == 0

    def test_queue_pos_ordering(self, client):
        t0 = time.time()
        flask_app.jobs["qa"] = {
            "status": "queued", "log": "", "output_dir": "x",
            "expires_at": None, "queued_at": t0,
        }
        flask_app.jobs["qb"] = {
            "status": "queued", "log": "", "output_dir": "x",
            "expires_at": None, "queued_at": t0 + 1,
        }
        pos_a = json.loads(client.get("/status/qa/json").data)["queue_pos"]
        pos_b = json.loads(client.get("/status/qb/json").data)["queue_pos"]
        assert pos_a == 1
        assert pos_b == 2

    def test_multiple_dry_run_jobs_all_complete(self, client):
        data = {**VALID_FORM, "dry_run": "on"}
        r1 = client.post("/run", data=data, follow_redirects=False)
        r2 = client.post("/run", data=data, follow_redirects=False)
        r3 = client.post("/run", data=data, follow_redirects=False)

        def job_id(r):
            return r.headers["Location"].split("/status/")[1].strip("/")

        ids = [job_id(r1), job_id(r2), job_id(r3)]

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            statuses = [
                json.loads(client.get(f"/status/{jid}/json").data)["status"]
                for jid in ids
            ]
            if all(s == "done" for s in statuses):
                break
            time.sleep(0.1)

        assert all(s == "done" for s in statuses)
        for jid in ids:
            shutil.rmtree(Path(flask_app.jobs[jid]["output_dir"]), ignore_errors=True)


# ── Rate limiting ──────────────────────────────────────────────────────────────

class TestRateLimit:
    def test_run_second_job_blocked(self, client_limited):
        with patch("app._save_job"), patch("app._job_queue.put"):
            r1 = client_limited.post("/run", data=VALID_FORM, follow_redirects=False)
            r2 = client_limited.post("/run", data=VALID_FORM, follow_redirects=False)
        assert r1.status_code == 302
        assert r2.status_code == 429

    def test_register_blocked_after_three(self, anon_client_limited):
        for _ in range(3):
            anon_client_limited.post("/register")
        r = anon_client_limited.post("/register")
        assert r.status_code == 429

    def test_login_blocked_after_twenty(self, anon_client_limited):
        for _ in range(20):
            anon_client_limited.post("/login", data={"username": "x", "password": "x"})
        r = anon_client_limited.post("/login", data={"username": "x", "password": "x"})
        assert r.status_code == 429


# ── Single lift mode ───────────────────────────────────────────────────────────

SINGLE_FORM_BASE = {
    "url":              "https://www.youtube.com/watch?v=test",
    "single_lift":      "on",
    "single_timestamp": "0:21:27",
    "single_movement":  "squat",
    "single_attempt":   "3",
    "duration":         "60",
    "audio_mode":       "original",
    "dry_run":          "on",
}


class TestSingleLift:
    def _submit(self, client, overrides=None):
        data = {**SINGLE_FORM_BASE, **(overrides or {})}
        return client.post("/run", data=data, follow_redirects=False)

    def _job_id(self, r):
        return r.headers["Location"].split("/status/")[1].strip("/")

    def _poll(self, client, job_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(f"/status/{job_id}/json")
            if json.loads(r.data)["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        return json.loads(client.get(f"/status/{job_id}/json").data)

    def test_missing_timestamp_returns_400(self, client):
        r = self._submit(client, {"single_timestamp": ""})
        assert r.status_code == 400
        assert b"single_timestamp" in r.data or b"is-invalid" in r.data

    def test_missing_music_returns_400(self, client):
        r = self._submit(client, {"audio_mode": "music_only", "music": ""})
        assert r.status_code == 400

    def test_dispatches_as_single_mode(self, client):
        queued = []
        with patch("app._save_job"), patch("app._job_queue.put", side_effect=queued.append):
            self._submit(client)
        assert queued, "nothing was queued"
        _job_id, _run_kwargs, mode = queued[0]
        assert mode == "single"

    def test_dry_run_original_creates_one_file(self, client):
        r = self._submit(client)
        job_id = self._job_id(r)
        self._poll(client, job_id)
        out_dir = Path(flask_app.jobs[job_id]["output_dir"])
        mp4s = list(out_dir.rglob("*.mp4"))
        assert len(mp4s) == 1
        assert mp4s[0].name == "squat_attempt3_original.mp4"
        shutil.rmtree(out_dir, ignore_errors=True)

    def test_dry_run_music_only_creates_two_files(self, client):
        r = self._submit(client, {"audio_mode": "music_only", "music": "test song"})
        job_id = self._job_id(r)
        self._poll(client, job_id)
        out_dir = Path(flask_app.jobs[job_id]["output_dir"])
        mp4s = sorted(f.name for f in out_dir.rglob("*.mp4"))
        assert mp4s == ["squat_attempt3_music.mp4", "squat_attempt3_original.mp4"]
        shutil.rmtree(out_dir, ignore_errors=True)

    def test_dry_run_mixed_creates_three_files(self, client):
        r = self._submit(client, {"audio_mode": "mixed", "music": "test song"})
        job_id = self._job_id(r)
        self._poll(client, job_id)
        out_dir = Path(flask_app.jobs[job_id]["output_dir"])
        mp4s = sorted(f.name for f in out_dir.rglob("*.mp4"))
        assert mp4s == [
            "squat_attempt3_mixed.mp4",
            "squat_attempt3_music.mp4",
            "squat_attempt3_original.mp4",
        ]
        shutil.rmtree(out_dir, ignore_errors=True)

    def test_preview_width_defaults_to_640_when_not_specified(self, client):
        queued = []
        with patch("app._save_job"), patch("app._job_queue.put", side_effect=queued.append):
            self._submit(client, {"preview": "on"})
        _job_id, run_kwargs, _mode = queued[0]
        assert run_kwargs["preview_width"] == 640

    def test_preview_width_explicit_value_respected(self, client):
        queued = []
        with patch("app._save_job"), patch("app._job_queue.put", side_effect=queued.append):
            self._submit(client, {"preview": "on", "preview_width": "320"})
        _job_id, run_kwargs, _mode = queued[0]
        assert run_kwargs["preview_width"] == 320


# ── _channel_whitelisted ───────────────────────────────────────────────────────

# ── Staging gate ──────────────────────────────────────────────────────────────

@pytest.fixture
def staging_client(tmp_path, monkeypatch):
    """Client that exercises the STAGING gate, logged in as a regular (non-admin) user."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("STAGING", "1")
    db.init_db()

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin)"
            " VALUES (?, ?, ?, 0)",
            ("regular", "Regular User", generate_password_hash("pass")),
        )
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin)"
            " VALUES (?, ?, ?, 1)",
            ("admin", "Admin User", generate_password_hash("adminpass")),
        )
        conn.commit()

    import limiter as limiter_mod
    flask_app.app.config["TESTING"] = True
    monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
    with flask_app.app.test_client() as c:
        yield c


class TestStagingGate:
    def _regular_id(self):
        with db.get_db() as conn:
            return conn.execute(
                "SELECT id FROM users WHERE username='regular'"
            ).fetchone()["id"]

    def test_anonymous_redirected_to_login(self, staging_client):
        r = staging_client.get("/")
        assert r.status_code in (302, 303)
        assert b"login" in r.headers["Location"].lower().encode()

    def test_user_without_access_gets_403(self, staging_client):
        staging_client.post("/login", data={"username": "regular", "password": "pass"})
        assert staging_client.get("/").status_code == 403

    def test_user_with_expired_access_gets_403(self, staging_client):
        staging_client.post("/login", data={"username": "regular", "password": "pass"})
        db.grant_staging_access(self._regular_id(), -3600)  # expires in the past
        assert staging_client.get("/").status_code == 403

    def test_user_with_valid_access_gets_200(self, staging_client):
        staging_client.post("/login", data={"username": "regular", "password": "pass"})
        db.grant_staging_access(self._regular_id(), 3600)
        assert staging_client.get("/").status_code == 200

    def test_admin_always_allowed(self, staging_client):
        staging_client.post("/login", data={"username": "admin", "password": "adminpass"})
        assert staging_client.get("/").status_code == 200


class TestStagingAdminRoutes:
    def test_grant_creates_access(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin)"
                " VALUES (?, ?, ?, 0)",
                ("u", "U", generate_password_hash("p")),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]

        client.post(f"/admin/staging/grant/{user_id}", data={"duration": "8h"})
        expiry = db.get_staging_access(user_id)
        assert expiry is not None
        assert expiry > time.time()

    def test_revoke_removes_access(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin)"
                " VALUES (?, ?, ?, 0)",
                ("u", "U", generate_password_hash("p")),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]

        db.grant_staging_access(user_id, 3600)
        client.post(f"/admin/staging/revoke/{user_id}")
        assert db.get_staging_access(user_id) is None


# ── _channel_whitelisted ───────────────────────────────────────────────────────

class TestChannelWhitelisted:
    def test_known_handle_returns_true(self):
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@powerliftingaep4634"
        )

    def test_unknown_handle_returns_false(self):
        assert not flask_app._channel_whitelisted(
            "https://www.youtube.com/@SomeRandomChannel"
        )

    def test_match_is_case_insensitive(self):
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@POWERLIFTINGAEP4634"
        )
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@POWERLIFTINGTV"
        )

    def test_new_federations_are_whitelisted(self):
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@USAPowerlifting1"
        )
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@canadapowerlifting"
        )
        assert flask_app._channel_whitelisted(
            "https://www.youtube.com/@gbpowerfed-britishpowerlifting"
        )

class TestChannelSearch:
    YT_RESULT = json.dumps({
        "title": "Young Ambition Cup II",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "url": "https://www.youtube.com/watch?v=abc123",
        "duration": 7200,
    })
    PRIVATE_RESULT = json.dumps({
        "title": "[Private video]",
        "webpage_url": "https://www.youtube.com/watch?v=prv",
        "url": "https://www.youtube.com/watch?v=prv",
        "duration": None,
    })

    def _search(self, client, source="aep", q="young ambition"):
        return client.get(f"/api/channel-search?source={source}&q={q}")

    def test_unknown_source_returns_400(self, client):
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        r = self._search(client, source="unknown")
        assert r.status_code == 400

    def test_returns_results(self, client):
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        mock_result = type("R", (), {"stdout": self.YT_RESULT + "\n", "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result):
            r = self._search(client)
        assert r.status_code == 200
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Young Ambition Cup II"
        assert "url" in data[0]

    def test_filters_private_videos(self, client):
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        stdout = self.YT_RESULT + "\n" + self.PRIVATE_RESULT + "\n"
        mock_result = type("R", (), {"stdout": stdout, "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result):
            r = self._search(client)
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Young Ambition Cup II"

    def test_filters_off_topic_results(self, client):
        # YouTube sometimes returns unrelated videos; filter by query word in title
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        off_topic = json.dumps({
            "title": "I Open del Mediterrani Sesión 2",
            "webpage_url": "https://www.youtube.com/watch?v=offtopic",
            "url": "https://www.youtube.com/watch?v=offtopic",
            "duration": 3600,
        })
        stdout = self.YT_RESULT + "\n" + off_topic + "\n"
        mock_result = type("R", (), {"stdout": stdout, "returncode": 0})()
        with patch("subprocess.run", return_value=mock_result):
            r = self._search(client, q="young")
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["title"] == "Young Ambition Cup II"

    def test_empty_query_returns_empty_list(self, client):
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        r = client.get("/api/channel-search?source=aep&q=")
        assert r.status_code == 200

    def test_new_sources_are_valid(self, client):
        client.post("/login", data={"username": "admin", "password": "adminpass"})
        for source in ("usapl", "bp", "cpu"):
            r = client.get(f"/api/channel-search?source={source}&q=")
            assert r.status_code == 200, f"source={source} returned {r.status_code}"
        assert r.get_json() == []


class TestHistory:
    def test_history_returns_200(self, client):
        assert client.get("/history").status_code == 200

    def test_history_empty_for_new_user(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(flask_app, "Path", lambda p: tmp_path / p if p == "lifts" else __import__("pathlib").Path(p))
        r = client.get("/history")
        assert r.status_code == 200

    def test_history_shows_own_jobs(self, client, tmp_path):
        lifts = tmp_path / "lifts"
        job_dir = lifts / "abcd1234"
        job_dir.mkdir(parents=True)
        import time as _time
        status = {
            "job_id": "abcd1234deadbeef",
            "status": "done",
            "log": "",
            "output_dir": str(job_dir),
            "expires_at": _time.time() + 3600,
            "mode": "full",
            "user_id": "1",
            "submitted_url": "https://www.youtube.com/watch?v=test",
            "queued_at": _time.time(),
            "expired": False,
        }
        (job_dir / "status.json").write_text(__import__("json").dumps(status))
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "app.Path", side_effect=lambda p: tmp_path / p if p == "lifts" else __import__("pathlib").Path(p)
        ):
            r = client.get("/history")
        assert r.status_code == 200
        assert b"youtube.com" in r.data

    def test_history_excludes_other_users_jobs(self, client, tmp_path):
        lifts = tmp_path / "lifts"
        job_dir = lifts / "other1234"
        job_dir.mkdir(parents=True)
        status = {
            "job_id": "other1234deadbeef",
            "status": "done",
            "log": "",
            "output_dir": str(job_dir),
            "expires_at": None,
            "mode": "full",
            "user_id": "999",
            "submitted_url": "https://www.youtube.com/watch?v=other",
            "queued_at": 0,
        }
        (job_dir / "status.json").write_text(__import__("json").dumps(status))
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "app.Path", side_effect=lambda p: tmp_path / p if p == "lifts" else __import__("pathlib").Path(p)
        ):
            r = client.get("/history")
        assert b"watch?v=other" not in r.data

    def test_history_requires_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flask_app.app, "config", {**flask_app.app.config, "TESTING": True})
        with flask_app.app.test_client() as c:
            r = c.get("/history", follow_redirects=False)
        assert r.status_code in (302, 401)


# ── CAPTCHA (altcha) ───────────────────────────────────────────────────────────

class TestCaptcha:
    def test_challenge_endpoint_returns_valid_json(self, client):
        r = client.get("/api/captcha-challenge")
        assert r.status_code == 200
        data = r.get_json()
        assert "algorithm" in data
        assert "challenge" in data
        assert "maxnumber" in data
        assert "salt" in data
        assert "signature" in data

    def test_register_skips_captcha_in_testing_mode(self, tmp_path, monkeypatch):
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "cap.db")
        db_mod.init_db()
        with flask_app.app.test_client() as c:
            r = c.post("/register", follow_redirects=False)
        assert r.status_code == 200
        with db_mod.get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0]
        assert count == 1

    def test_register_rejects_missing_captcha_outside_testing(self, tmp_path, monkeypatch):
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "cap2.db")
        db_mod.init_db()
        non_testing_config = {**flask_app.app.config, "TESTING": False}
        monkeypatch.setattr(flask_app.app, "config", non_testing_config)
        with flask_app.app.test_client() as c:
            r = c.post("/register")
        assert r.status_code == 200
        assert b"alert-danger" in r.data
        with db_mod.get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0]
        assert count == 0


# ── job_stats DB layer ────────────────────────────────────────────────────────

class TestJobStats:
    def test_record_and_retrieve(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        now = time.time()
        db.record_job_stat(
            submitted_at=now - 10, started_at=now - 8, finished_at=now,
            status="done", source="aep", mode="full", has_music=1,
        )
        stats = db.get_stats(days=30)
        assert stats["summary"]["total"] == 1
        assert stats["summary"]["success"] == 1
        assert stats["by_source"][0]["source"] == "aep"
        assert stats["by_music"][0]["has_music"] == 1

    def test_filter_by_days_excludes_old(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        now = time.time()
        db.record_job_stat(submitted_at=now - 10 * 86400, started_at=None,
                           finished_at=None, status="done")
        db.record_job_stat(submitted_at=now - 1 * 86400, started_at=None,
                           finished_at=None, status="error")
        stats = db.get_stats(days=7)
        assert stats["summary"]["total"] == 1
        assert stats["summary"]["success"] == 0

    def test_get_stats_all_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        now = time.time()
        db.record_job_stat(submitted_at=now - 100 * 86400, started_at=None,
                           finished_at=None, status="done")
        stats = db.get_stats(days=None)
        assert stats["summary"]["total"] == 1

    def test_by_hour_has_24_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        stats = db.get_stats(days=30)
        assert len(stats["by_hour"]) == 24

    def test_worker_records_stat_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        job = {"status": "running", "log": "", "output_dir": str(tmp_path),
               "queued_at": time.time(), "mode": "full", "source": "ipf", "_geo": {}}
        job_id = "stattest1"
        flask_app.jobs[job_id] = job
        with patch("app.run"), patch("app._save_job"), \
             patch("app._resolve_channel_url", return_value=""):
            flask_app._worker(job_id, job, {"url": "u", "music_source": ""}, "full")
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM job_stats").fetchone()
        assert row["status"] == "done"
        assert row["source"] == "ipf"
        assert row["has_music"] == 0

    def test_worker_records_stat_on_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        job = {"status": "running", "log": "", "output_dir": str(tmp_path),
               "queued_at": time.time(), "mode": "single", "source": "aep", "_geo": {}}
        job_id = "stattest2"
        flask_app.jobs[job_id] = job
        with patch("app.run", side_effect=Exception("boom")), \
             patch("app._save_job"), \
             patch("app._resolve_channel_url", return_value=""):
            flask_app._worker(job_id, job, {"url": "u", "music_source": "song"}, "full")
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM job_stats").fetchone()
        assert row["status"] == "error"
        assert row["has_music"] == 1


# ── /admin/stats ──────────────────────────────────────────────────────────────

class TestStatsPage:
    def test_admin_gets_200(self, client):
        assert client.get("/admin/stats").status_code == 200

    def test_filter_7d(self, client):
        assert client.get("/admin/stats?days=7").status_code == 200

    def test_filter_all_time(self, client):
        assert client.get("/admin/stats?days=0").status_code == 200

    def test_requires_admin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin)"
                " VALUES (?, ?, ?, 0)",
                ("user1", "User One", generate_password_hash("pass")),
            )
            conn.commit()
        import limiter as limiter_mod
        flask_app.app.config["TESTING"] = True
        monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
        with flask_app.app.test_client() as c:
            c.post("/login", data={"username": "user1", "password": "pass"})
            r = c.get("/admin/stats", follow_redirects=False)
        assert r.status_code == 302

    def test_page_renders_with_data(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        db.record_job_stat(submitted_at=time.time() - 3600, started_at=time.time() - 3590,
                           finished_at=time.time() - 100, status="done",
                           source="aep", mode="full", has_music=0)
        r = client.get("/admin/stats")
        assert r.status_code == 200
        assert "Estadísticas".encode() in r.data


# ── geoip ─────────────────────────────────────────────────────────────────────

class TestGeoip:
    def test_lookup_no_db(self, tmp_path, monkeypatch):
        import geoip
        monkeypatch.setattr(geoip, "_GEOIP_PATH", str(tmp_path / "nonexistent.mmdb"))
        monkeypatch.setattr(geoip, "_reader", None)
        monkeypatch.setattr(geoip, "_reader_tried", False)
        assert geoip.lookup("8.8.8.8") == {}

    def test_lookup_invalid_file(self, tmp_path, monkeypatch):
        import geoip
        bad = tmp_path / "bad.mmdb"
        bad.write_bytes(b"not a real database")
        monkeypatch.setattr(geoip, "_GEOIP_PATH", str(bad))
        monkeypatch.setattr(geoip, "_reader", None)
        monkeypatch.setattr(geoip, "_reader_tried", False)
        assert geoip.lookup("8.8.8.8") == {}


# ── /feedback ─────────────────────────────────────────────────────────────────

def _make_user(conn, username, is_admin=0):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin)"
        " VALUES (?, ?, ?, ?)",
        (username, username.replace("_", " ").title(),
         generate_password_hash("pass"), is_admin),
    )
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]


def _login_as(tmp_path, monkeypatch, username, is_admin=0):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    import limiter as limiter_mod
    flask_app.app.config["TESTING"] = True
    monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
    with db.get_db() as conn:
        _make_user(conn, username, is_admin)
    c = flask_app.app.test_client()
    c.post("/login", data={"username": username, "password": "pass"})
    return c


_MOCK_ISSUE = {"number": 42, "html_url": "https://github.com/test/repo/issues/42"}


class TestFeedback:
    def test_page_returns_200(self, client):
        assert client.get("/feedback").status_code == 200

    def test_requires_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        flask_app.app.config["TESTING"] = True
        with flask_app.app.test_client() as c:
            r = c.get("/feedback", follow_redirects=False)
        assert r.status_code == 302

    def test_submit_creates_db_row(self, client):
        with patch("github_issues.create_issue", return_value=_MOCK_ISSUE):
            r = client.post("/feedback",
                            data={"body": "Estaría bien poder elegir el intento"},
                            follow_redirects=False)
        assert r.status_code == 302
        rows = db.get_feedback_for_user(1)
        assert len(rows) == 1
        assert rows[0]["issue_number"] == 42
        assert rows[0]["status"] == "open"

    def test_empty_body_returns_400(self, client):
        r = client.post("/feedback", data={"body": ""})
        assert r.status_code == 400

    def test_body_too_long_returns_400(self, client):
        r = client.post("/feedback", data={"body": "x" * 2001})
        assert r.status_code == 400

    def test_github_error_shows_error_no_db_row(self, client):
        with patch("github_issues.create_issue", side_effect=RuntimeError("API error")):
            r = client.post("/feedback", data={"body": "Texto de prueba"})
        assert r.status_code == 502
        assert db.get_feedback_for_user(1) == []

    def test_panel_shows_own_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        import limiter as limiter_mod
        flask_app.app.config["TESTING"] = True
        monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
        with db.get_db() as conn:
            uid_a = _make_user(conn, "user_a")
            uid_b = _make_user(conn, "user_b")
        db.add_feedback(uid_a, 10, "https://gh/10", "Feedback de A", "excerpt a")
        db.add_feedback(uid_b, 11, "https://gh/11", "Feedback de B", "excerpt b")
        with flask_app.app.test_client() as c:
            c.post("/login", data={"username": "user_a", "password": "pass"})
            r = c.get("/feedback")
        assert b"Feedback de A" in r.data
        assert b"Feedback de B" not in r.data


class TestAdminFeedback:
    def test_shows_all_feedbacks(self, client):
        with db.get_db() as conn:
            uid_a = _make_user(conn, "user_a")
            uid_b = _make_user(conn, "user_b")
        db.add_feedback(uid_a, 10, "https://gh/10", "Feedback de A", "excerpt a")
        db.add_feedback(uid_b, 11, "https://gh/11", "Feedback de B", "excerpt b")
        r = client.get("/admin/feedback")
        assert r.status_code == 200
        assert b"Feedback de A" in r.data
        assert b"Feedback de B" in r.data

    def test_requires_admin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        import limiter as limiter_mod
        flask_app.app.config["TESTING"] = True
        monkeypatch.setattr(limiter_mod.limiter, "enabled", False)
        with db.get_db() as conn:
            _make_user(conn, "normaluser", is_admin=0)
        with flask_app.app.test_client() as c:
            c.post("/login", data={"username": "normaluser", "password": "pass"})
            r = c.get("/admin/feedback", follow_redirects=False)
        assert r.status_code == 302

    def test_filter_by_status(self, client):
        with db.get_db() as conn:
            uid = _make_user(conn, "user_a")
        db.add_feedback(uid, 10, "https://gh/10", "Open feedback", "excerpt")
        db.add_feedback(uid, 11, "https://gh/11", "Closed feedback", "excerpt")
        db.update_feedback_from_github(11, "closed", "[]")
        r = client.get("/admin/feedback?status=open")
        assert b"Open feedback" in r.data
        assert b"Closed feedback" not in r.data


class TestGithubWebhook:
    def _make_sig(self, body: bytes, secret: str) -> str:
        import hashlib
        import hmac as hmac_mod
        return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def _post_webhook(self, client, monkeypatch, secret, issue_number, action,
                      state_reason=None, labels=None):
        import github_issues as gi
        monkeypatch.setattr(gi, "_SECRET_PATH",
                            type("P", (), {"read_text": lambda s: secret})())
        issue = {"number": issue_number, "labels": [{"name": l} for l in (labels or [])]}
        if state_reason:
            issue["state_reason"] = state_reason
        payload = json.dumps({"action": action, "issue": issue}).encode()
        sig = self._make_sig(payload, secret)
        return client.post("/webhook/github", data=payload,
                           content_type="application/json",
                           headers={"X-GitHub-Event": "issues",
                                    "X-Hub-Signature-256": sig})

    def test_updates_status_on_close(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            uid = _make_user(conn, "user_a")
        db.add_feedback(uid, 99, "https://gh/99", "Test feedback", "excerpt")
        r = self._post_webhook(client, monkeypatch, "testsecret", 99, "closed",
                               labels=["user-feedback", "bug"])
        assert r.status_code == 204
        assert db.get_feedback_for_user(uid)[0]["status"] == "closed"

    def test_updates_status_not_planned(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            uid = _make_user(conn, "user_a")
        db.add_feedback(uid, 99, "https://gh/99", "Test feedback", "excerpt")
        r = self._post_webhook(client, monkeypatch, "testsecret", 99, "closed",
                               state_reason="not_planned")
        assert r.status_code == 204
        assert db.get_feedback_for_user(uid)[0]["status"] == "wontfix"

    def test_updates_status_duplicate(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        with db.get_db() as conn:
            uid = _make_user(conn, "user_a")
        db.add_feedback(uid, 99, "https://gh/99", "Test feedback", "excerpt")
        r = self._post_webhook(client, monkeypatch, "testsecret", 99, "closed",
                               state_reason="duplicate")
        assert r.status_code == 204
        assert db.get_feedback_for_user(uid)[0]["status"] == "duplicate"

    def test_rejects_bad_signature(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        secret = "testsecret"
        import github_issues as gi
        monkeypatch.setattr(gi, "_SECRET_PATH",
                            type("P", (), {"read_text": lambda s: secret})())
        payload = b'{"action": "closed", "issue": {"number": 1, "labels": []}}'
        r = client.post("/webhook/github",
                        data=payload,
                        content_type="application/json",
                        headers={"X-GitHub-Event": "issues",
                                 "X-Hub-Signature-256": "sha256=badvalue"})
        assert r.status_code == 400

    def test_ignores_non_issue_events(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        secret = "testsecret"
        import github_issues as gi
        monkeypatch.setattr(gi, "_SECRET_PATH",
                            type("P", (), {"read_text": lambda s: secret})())
        payload = b'{"action": "created"}'
        sig = self._make_sig(payload, secret)
        r = client.post("/webhook/github",
                        data=payload,
                        content_type="application/json",
                        headers={"X-GitHub-Event": "pull_request",
                                 "X-Hub-Signature-256": sig})
        assert r.status_code == 204


# ── /run-ocr ──────────────────────────────────────────────────────────────────

OCR_FORM_BASE = {
    "url":          "https://www.youtube.com/watch?v=test",
    "ocr_apellido": "OSUNA",
}


class TestRunOcr:
    def _post(self, client, overrides=None):
        data = {**OCR_FORM_BASE, **(overrides or {})}
        with patch("app._save_job"), patch("app._job_queue.put"):
            return client.post("/run-ocr", data=data, follow_redirects=False)

    def test_valid_form_redirects_to_status(self, client):
        r = self._post(client)
        assert r.status_code == 302
        assert "/status/" in r.headers["Location"]

    def test_valid_form_creates_queued_ocr_job(self, client):
        self._post(client)
        assert len(flask_app.jobs) == 1
        job = next(iter(flask_app.jobs.values()))
        assert job["status"] == "queued"
        assert job["mode"] == "ocr"

    def test_missing_url_returns_400(self, client):
        assert self._post(client, {"url": ""}).status_code == 400

    def test_missing_apellido_returns_400(self, client):
        assert self._post(client, {"ocr_apellido": ""}).status_code == 400

    def test_queued_job_has_apellido(self, client):
        self._post(client)
        job = next(iter(flask_app.jobs.values()))
        assert job["ocr_apellido"] == "OSUNA"

    def test_queues_with_ocr_mode(self, client):
        queued = []
        with patch("app._save_job"), patch("app._job_queue.put", side_effect=queued.append):
            client.post("/run-ocr", data=OCR_FORM_BASE, follow_redirects=False)
        assert queued
        _job_id, _run_kwargs, mode = queued[0]
        assert mode == "ocr"


# ── /ocr/<job_id>/review ──────────────────────────────────────────────────────

OCR_RESULT = {
    "squat":    [1287, 1795, 2295],
    "bench":    [5010, 5790, 6570],
    "deadlift": [9630, 10164, 10698],
    "comp_start": 1257,
    "elapsed_s": 1560.0,
}


class TestOcrReview:
    def _make_ocr_done_job(self, job_id="ocrtestjob"):
        flask_app.jobs[job_id] = {
            "status": "ocr_done",
            "log": "",
            "output_dir": "lifts/ocrtestj",
            "expires_at": time.time() + 3600,
            "mode": "ocr",
            "user_id": "1",
            "submitted_url": "https://www.youtube.com/watch?v=test",
            "source": "aep",
            "session_label": "Test session",
            "ocr_result": OCR_RESULT,
            "ocr_apellido": "OSUNA",
        }
        return job_id

    def test_ocr_done_job_returns_200(self, client):
        job_id = self._make_ocr_done_job()
        r = client.get(f"/ocr/{job_id}/review")
        assert r.status_code == 200

    def test_review_page_shows_timestamps(self, client):
        job_id = self._make_ocr_done_job()
        r = client.get(f"/ocr/{job_id}/review")
        assert b"0:21:27" in r.data  # squat[0] = 1287s

    def test_review_page_has_adjust_buttons(self, client):
        job_id = self._make_ocr_done_job()
        r = client.get(f"/ocr/{job_id}/review")
        assert b"\xe2\x88\x925s" in r.data or b"5s" in r.data  # −5s or +5s

    def test_missing_job_returns_404(self, client):
        with patch("app._load_job", return_value=None):
            r = client.get("/ocr/nonexistentjob/review")
        assert r.status_code == 404

    def test_still_running_job_returns_404(self, client):
        flask_app.jobs["runningjob"] = {
            "status": "running", "log": "", "output_dir": "x",
            "expires_at": None, "mode": "ocr", "ocr_result": None,
        }
        r = client.get("/ocr/runningjob/review")
        assert r.status_code == 404

    def test_status_json_includes_ocr_result(self, client):
        job_id = self._make_ocr_done_job()
        r = client.get(f"/status/{job_id}/json")
        data = json.loads(r.data)
        assert "ocr_result" in data
        assert data["ocr_result"]["squat"] == OCR_RESULT["squat"]


# ── _ocr_worker unit tests ─────────────────────────────────────────────────────

class TestOcrWorker:
    def _make_job(self, tmp_path):
        return {
            "status": "running",
            "log": "",
            "output_dir": str(tmp_path / "ocr_job"),
            "queued_at": time.time(),
            "mode": "ocr",
            "source": "aep",
            "submitted_url": "https://www.youtube.com/watch?v=test",
            "ocr_apellido": "OSUNA",
            "_geo": {},
            "ocr_result": None,
        }

    def test_ocr_worker_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        job = self._make_job(tmp_path)
        fake_result = json.dumps(OCR_RESULT)

        import subprocess as sp
        mock_proc = type("P", (), {
            "stdout": iter([fake_result]),
            "stderr": iter(["Fase 1: inicio\nFase 6: peso muerto\nTerminado en 100s\n"]),
            "returncode": 0,
            "communicate": lambda self: (fake_result, ""),
        })()

        with patch("app._save_job"), patch("subprocess.Popen", return_value=mock_proc):
            flask_app._ocr_worker("ocr1", job)

        assert job["status"] == "ocr_done"
        assert job["ocr_result"]["squat"] == OCR_RESULT["squat"]

    def test_ocr_worker_subprocess_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.init_db()
        job = self._make_job(tmp_path)

        mock_proc = type("P", (), {
            "stdout": iter([]),
            "stderr": iter(["Error fatal\n"]),
            "returncode": 1,
            "communicate": lambda self: ("", "Error fatal"),
        })()

        with patch("app._save_job"), patch("subprocess.Popen", return_value=mock_proc):
            flask_app._ocr_worker("ocr2", job)

        assert job["status"] == "error"
