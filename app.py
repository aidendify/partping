"""PartPing: hosted parts-status timeline for local service owners."""

from __future__ import annotations

import secrets

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.secret_key = __import__("os").environ.get("SECRET_KEY", "partping-self-hosted-change-me")

app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "sms_configured": H.sms_configured(),
        "business_name": H.business_name(),
        "business_phone": H.business_phone(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "milestone_label": H.milestone_label,
        "format_eta_display": H.format_eta_display,
        "format_eta_short": H.format_eta_short,
        "MILESTONES": H.MILESTONES,
        "MILESTONE_LABELS": H.MILESTONE_LABELS,
        "TERMINAL_MILESTONES": H.TERMINAL_MILESTONES,
        "MILESTONE_ORDER": H.MILESTONE_ORDER,
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "sms_configured": H.sms_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    rows = H.get_db().execute(
        """
        SELECT * FROM jobs
        WHERE milestone NOT IN ('closed', 'cancelled')
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    return render_template("index.html", jobs=rows, closed_view=False)


@app.get("/closed")
def closed_jobs():
    rows = H.get_db().execute(
        """
        SELECT * FROM jobs
        WHERE milestone IN ('closed', 'cancelled')
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    return render_template("index.html", jobs=rows, closed_view=True)


PLACEHOLDER_REST_OF_APP
