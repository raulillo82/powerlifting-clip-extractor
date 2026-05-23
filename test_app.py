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

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        c.post("/login", data={"username": "testadmin", "password": "testpass"})
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
        with patch("app._save_job"), patch("app.threading.Thread"):
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

    def test_valid_form_creates_running_job(self, client):
        self._post(client)
        assert len(flask_app.jobs) == 1
        assert next(iter(flask_app.jobs.values()))["status"] == "running"

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

    def _poll(self, client, job_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(f"/status/{job_id}/json")
            if json.loads(r.data)["status"] != "running":
                break
            time.sleep(0.05)
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
