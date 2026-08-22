#!/usr/bin/env python3
"""
auth.py — session login, registration, password change, role gating.

FOR PART 2 / A COLD READER
--------------------------
  * Session-based auth using Flask's signed cookie session (no Flask-Login
    dependency — one small module is easier to audit and adds nothing to
    install).  The logged-in user id lives in session["user_id"].
  * `current_user()` returns a sqlite3.Row or None.  `g.user` is populated by
    the before_request hook in app.py.
  * Decorators you will want:
        @login_required          -> 401/redirect for anonymous
        @role_required("admin")  -> 403 FORBIDDEN (not a silent redirect)
    The 403 behaviour is deliberate and tested: a logged-in non-admin hitting
    an admin route must get a real Forbidden, not a confusing bounce to login.
  * Rate limiting is a fixed-window counter in the login_attempts table
    (config.LOGIN_MAX_ATTEMPTS per config.LOGIN_WINDOW_SECONDS, keyed by
    username+IP).  Successful login clears the counter.
  * must_change_password forces a redirect to /change-password on every route
    until the user sets a new one.  The seeded admin ships with this set, so
    the credential documented in the README cannot survive first use.

PASSWORD HASHING IS NOT FILE ENCRYPTION.  Account passwords are hashed with
werkzeug PBKDF2.  No file data is ever encrypted — the thesis Delimitations
exclude encryption of compressed output.  See SCOPE_NOTES.md.
"""
import functools
import hmac
import re
import secrets
from urllib.parse import urlsplit

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash

import config
import db

bp = Blueprint("auth", __name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    if session.get("auth_epoch") != db.session_epoch():
        session.clear()
        return None
    user = db.get_user_by_id(uid)
    # Account changes take effect for already-issued cookies.  A disabled or
    # deleted account must not retain access until the session expires.
    if user is None or not user["is_active"]:
        session.clear()
        return None
    return user


def client_ip():
    # Do not trust a caller-supplied X-Forwarded-For value.  Deployments behind
    # a trusted proxy can install Werkzeug's ProxyFix with an explicit hop
    # count; the local application uses the peer address directly.
    return request.remote_addr or ""


def csrf_token():
    """Return the per-session CSRF token used by forms and same-origin JS."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_is_valid(value):
    expected = session.get("_csrf_token")
    return bool(expected and value and hmac.compare_digest(expected, value))


def safe_next(value):
    """Return a same-site absolute path, or an empty string."""
    if not value or "\\" in value:
        return ""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path.startswith("/") \
            or parts.path.startswith("//"):
        return ""
    return value


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if getattr(g, "user", None) is None:
            if request.accept_mimetypes.best == "application/json" or \
                    request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for(
                "auth.login", next=request.full_path.rstrip("?")))
        return view(*a, **kw)
    return wrapped


def role_required(*roles):
    """Gate a route on role.  Anonymous -> login redirect; wrong role -> 403.

    The 403 (rather than a redirect) is required by the brief so that a
    permission failure is distinguishable from a session timeout."""
    def deco(view):
        @functools.wraps(view)
        def wrapped(*a, **kw):
            user = getattr(g, "user", None)
            if user is None:
                if request.path.startswith("/api/"):
                    abort(401)
                return redirect(url_for(
                    "auth.login", next=request.full_path.rstrip("?")))
            if user["role"] not in roles:
                db.audit("forbidden", user_id=user["id"],
                         username=user["username"],
                         detail="role=%s path=%s" % (user["role"], request.path),
                         ip_address=client_ip())
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def validate_registration(username, email, password, confirm):
    """Returns an error string, or None when the input is acceptable."""
    if not _USERNAME_RE.match(username or ""):
        return ("Username must be 3–32 characters, letters/digits/._- only.")
    if not _EMAIL_RE.match(email or ""):
        return "Enter a valid email address."
    if len(password or "") < config.MIN_PASSWORD_LENGTH:
        return ("Password must be at least %d characters."
                % config.MIN_PASSWORD_LENGTH)
    if password != confirm:
        return "The two passwords do not match."
    if db.get_user_by_username(username):
        return "That username is already taken."
    if db.get_user_by_email(email):
        return "That email is already registered."
    return None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ip = client_ip()

    # Fixed-window rate limit BEFORE any password check, so a locked-out
    # attacker learns nothing about whether the username exists.
    if db.is_rate_limited(username, ip):
        db.audit("rate_limited", username=username,
                 detail="login blocked", ip_address=ip)
        return render_template(
            "login.html",
            error="Too many failed attempts. Try again in %d minutes."
                  % max(1, config.LOGIN_WINDOW_SECONDS // 60)), 429

    user = db.get_user_by_username(username)
    if user is None or not check_password_hash(user["password_hash"], password):
        db.record_login_failure(username, ip)
        db.audit("login_failed", user_id=user["id"] if user else None,
                 username=username, ip_address=ip)
        remaining = max(0, config.LOGIN_MAX_ATTEMPTS
                        - db.login_attempts_in_window(username, ip))
        return render_template(
            "login.html",
            error="Incorrect username or password. %d attempt(s) remaining."
                  % remaining), 401

    if not user["is_active"]:
        db.audit("login_failed", user_id=user["id"], username=username,
                 detail="account disabled", ip_address=ip)
        return render_template(
            "login.html", error="That account has been disabled."), 403

    db.clear_login_failures(username, ip)
    session.clear()
    session["user_id"] = user["id"]
    session["auth_epoch"] = db.session_epoch()
    session.permanent = True
    db.touch_last_login(user["id"])
    db.audit("login", user_id=user["id"], username=username, ip_address=ip)

    nxt = safe_next(request.args.get("next") or request.form.get("next"))
    if user["must_change_password"]:
        if nxt:
            session["post_auth_next"] = nxt
        return redirect(url_for("auth.change_password"))
    if nxt:
        return redirect(nxt)
    # No intended destination: the workspace is the home of a signed-in
    # ByteSize account, not the public marketing page.
    return redirect(url_for("main.dashboard"))


@bp.post("/logout")
def logout():
    user = getattr(g, "user", None)
    if user is not None:
        db.audit("logout", user_id=user["id"], username=user["username"],
                 ip_address=client_ip())
    session.clear()
    return redirect(url_for("main.landing"))


@bp.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "GET":
        return render_template("register.html")
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    err = validate_registration(username, email, password, confirm)
    if err:
        return render_template("register.html", error=err,
                               username=username, email=email,
                               next_path=(request.args.get("next") or
                                          request.form.get("next") or "")), 400
    uid = db.create_user(username, email, password, role="user")
    db.audit("register", user_id=uid, username=username,
             ip_address=client_ip())
    session.clear()
    session["user_id"] = uid
    session["auth_epoch"] = db.session_epoch()
    session.permanent = True
    nxt = safe_next(request.args.get("next") or request.form.get("next"))
    if nxt:
        return redirect(nxt)
    return redirect(url_for("main.dashboard"))


@bp.route("/change-password", methods=("GET", "POST"))
@login_required
def change_password():
    user = g.user
    forced = bool(user["must_change_password"])
    if request.method == "GET":
        return render_template("change_password.html", forced=forced)
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    if not check_password_hash(user["password_hash"], current):
        return render_template("change_password.html", forced=forced,
                               error="Your current password is incorrect."), 400
    if len(new) < config.MIN_PASSWORD_LENGTH:
        return render_template(
            "change_password.html", forced=forced,
            error="New password must be at least %d characters."
                  % config.MIN_PASSWORD_LENGTH), 400
    if new != confirm:
        return render_template("change_password.html", forced=forced,
                               error="The two passwords do not match."), 400
    if new == current:
        return render_template(
            "change_password.html", forced=forced,
            error="The new password must differ from the current one."), 400
    db.set_password(user["id"], new)
    db.audit("password_change", user_id=user["id"], username=user["username"],
             ip_address=client_ip())
    flash("Password updated.")
    nxt = safe_next(session.pop("post_auth_next", None))
    if nxt:
        return redirect(nxt)
    return redirect(url_for("main.dashboard"))
