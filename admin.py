from flask import Blueprint, redirect, render_template, url_for
from auth import admin_required
from db import get_db

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/")
@admin_required
def index():
    with get_db() as conn:
        users = conn.execute("""
            SELECT *, datetime(created_at, 'unixepoch', 'localtime') as created_fmt
            FROM users WHERE is_admin = 0 ORDER BY created_at DESC
        """).fetchall()
    return render_template("admin/index.html", users=users)


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
