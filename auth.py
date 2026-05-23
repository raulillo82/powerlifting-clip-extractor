import secrets
import uuid
from functools import wraps

from flask import Blueprint, make_response, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()

MAX_USERS = 50

POWERLIFTER_NAMES = [
    # Women — Sheffield 2023-2026
    "agata_sitko", "alba_bostrom", "amanda_lawrence", "bonica_brown",
    "brittany_schlater", "carola_garra", "chandler_babb", "chiara_bernardi",
    "evie_corrigan", "heather_connor", "jade_jacob", "jessica_buettner",
    "joy_nnamani", "karlina_tongotea", "meghan_scanlon", "natalie_richards",
    "noemie_allabert", "prescillia_bavoil", "sara_naldi", "sonita_muluh",
    "tiffany_chapon", "ziana_azariah",
    # Men — Sheffield 2023-2026
    "abdul_sulayman", "ade_omisakin", "amar_kanane", "anatolii_novopismennyi",
    "anthony_mcnaughton", "ashton_rouska", "austin_perkins", "bobbie_butters",
    "carl_johansson", "carlos_petterson_grifith", "delaney_wallace",
    "eddie_berglund", "emil_krastev", "emil_norling", "etienne_el_chaer",
    "gavin_adin", "gustav_hedlund", "ivan_campano_diaz", "jesus_olivares",
    "jonathan_cayco", "joseph_borenstein", "jurins_kengamu", "kasemsand_senumong",
    "keenan_lee", "kjell_bakkelund", "kyota_ushiyama", "michael_davis",
    "panagiotis_tarinidis", "russel_orhii", "taylor_atwood", "timothy_monigatti",
    "tony_cliffe", "wascar_carpio",
]


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.display_name = row["display_name"]
        self.is_admin = bool(row["is_admin"])
        self._active = bool(row["is_active"])
        self.device_token = row["device_token"]

    @property
    def is_active(self):
        return self._active


@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("auth.login"))


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def _generate_password(length: int = 12) -> str:
    chars = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(chars) for _ in range(length))


def _display_name(username: str) -> str:
    return username.replace("_", " ").title()


# ── Routes ─────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    prefill = request.args.get("username", "")
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

        if not row or not check_password_hash(row["password_hash"], password):
            error = "Usuario o contraseña incorrectos."
        elif not row["is_active"]:
            error = "Cuenta desactivada. Contacta con el administrador."
        else:
            cookie_token = request.cookies.get("device_token")
            stored_token = row["device_token"]

            if stored_token and cookie_token != stored_token:
                error = "Esta cuenta está vinculada a otro dispositivo."
            else:
                new_token = stored_token or cookie_token or str(uuid.uuid4())
                if not stored_token:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE users SET device_token = ? WHERE id = ?",
                            (new_token, row["id"]),
                        )
                        conn.commit()

                login_user(User(row), remember=True)
                resp = make_response(
                    redirect(request.args.get("next") or url_for("index"))
                )
                resp.set_cookie("device_token", new_token,
                                max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
                return resp

    return render_template("login.html", error=error, prefill=prefill)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    cookie_token = request.cookies.get("device_token")

    if cookie_token:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE device_token = ?", (cookie_token,)
            ).fetchone()
        if existing:
            return render_template("register.html",
                                   already_registered=True,
                                   username=existing["username"],
                                   display_name=existing["display_name"])

    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 0"
        ).fetchone()[0]
        used = {r["username"] for r in
                conn.execute("SELECT username FROM users").fetchall()}

    slots_left = max(0, MAX_USERS - count)
    available = [n for n in POWERLIFTER_NAMES if n not in used]

    if request.method == "POST":
        if slots_left <= 0 or not available:
            return render_template("register.html", slots_left=0)

        username = secrets.choice(available)
        display = _display_name(username)
        password = _generate_password()
        device_token = cookie_token or str(uuid.uuid4())

        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, device_token)"
                " VALUES (?, ?, ?, ?)",
                (username, display, generate_password_hash(password), device_token),
            )
            conn.commit()

        resp = make_response(render_template(
            "register.html",
            created=True,
            username=username,
            display_name=display,
            password=password,
            slots_left=slots_left - 1,
        ))
        resp.set_cookie("device_token", device_token,
                        max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
        return resp

    return render_template("register.html", slots_left=slots_left)
