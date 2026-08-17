#!/usr/bin/env python3
"""
admin.py — admin-only views.

Scope is deliberately small for Part 1: a user list and the audit log.  Both
are gated with @auth.role_required("admin"), which returns a real 403 for a
logged-in non-admin (not a redirect) — this is asserted in the test suite.

Part 2 can add analytics here; keep new admin routes on this blueprint and
keep the role_required decorator on every one of them.
"""
from flask import Blueprint, g, render_template, request

import auth
import db

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/users")
@auth.role_required("admin")
def users():
    rows = db.list_users()
    db.audit("admin_view_users", user_id=g.user["id"],
             username=g.user["username"], ip_address=auth.client_ip())
    return render_template("admin_users.html", users=rows)


@bp.post("/users/<int:user_id>/active")
@auth.role_required("admin")
def toggle_active(user_id):
    """Enable/disable an account.  An admin cannot disable themselves — that
    would be an easy way to lock the only admin out of the local app."""
    target = db.get_user_by_id(user_id)
    if target is None:
        return render_template("error.html", code=404,
                               message="No such user."), 404
    if target["id"] == g.user["id"]:
        return render_template("error.html", code=400,
                               message="You cannot disable your own account."), 400
    new_state = request.form.get("active") == "1"
    db.set_user_active(user_id, new_state)
    db.audit("admin_toggle_active", user_id=g.user["id"],
             username=g.user["username"],
             detail="target=%s active=%s" % (target["username"], new_state),
             ip_address=auth.client_ip())
    from flask import redirect, url_for
    return redirect(url_for("admin.users"))


@bp.get("/audit")
@auth.role_required("admin")
def audit_log():
    return render_template("admin_audit.html", rows=db.list_audit(limit=300))
