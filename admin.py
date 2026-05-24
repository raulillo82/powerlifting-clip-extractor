import time

from flask import Blueprint, redirect, render_template, request, url_for
from auth import admin_required
from db import get_all_staging_access, get_db, grant_staging_access, revoke_staging_access

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

STAGING_DURATIONS = {
    "1h":  3_600,
    "8h":  28_800,
    "24h": 86_400,
    "3d":  259_200,
    "7d":  604_800,
}


@admin_bp.route("/")
@admin_required
def index():
    with get_db() as conn:
        users = conn.execute("""
            SELECT *, datetime(created_at, 'unixepoch', 'localtime') as created_fmt
            FROM users WHERE is_admin = 0 ORDER BY created_at DESC
        """).fetchall()
    staging = get_all_staging_access()
    now = time.time()
    return render_template("admin/index.html", users=users,
                           staging=staging, now=now,
                           staging_durations=STAGING_DURATIONS)


@admin_bp.route("/toggle/<int:user_id>", methods=["POST"])
@admin_required
def toggle(user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_active = NOT is_active WHERE id = ? AND is_admin = 0",
            (user_id,),
        )
        conn.commit()
    return redirect(url_for("admin.index"))


@admin_bp.route("/reset-device/<int:user_id>", methods=["POST"])
@admin_required
def reset_device(user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET device_token = NULL WHERE id = ? AND is_admin = 0",
            (user_id,),
        )
        conn.commit()
    return redirect(url_for("admin.index"))


@admin_bp.route("/delete/<int:user_id>", methods=["POST"])
@admin_required
def delete(user_id):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,)
        )
        conn.commit()
    return redirect(url_for("admin.index"))


@admin_bp.route("/staging/grant/<int:user_id>", methods=["POST"])
@admin_required
def staging_grant(user_id):
    duration_key = request.form.get("duration", "24h")
    seconds = STAGING_DURATIONS.get(duration_key, STAGING_DURATIONS["24h"])
    grant_staging_access(user_id, seconds)
    return redirect(url_for("admin.index"))


@admin_bp.route("/staging/revoke/<int:user_id>", methods=["POST"])
@admin_required
def staging_revoke(user_id):
    revoke_staging_access(user_id)
    return redirect(url_for("admin.index"))
