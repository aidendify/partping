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


@app.route("/jobs/new", methods=["GET", "POST"])
def new_job():
    if request.method == "GET":
        return render_template("new_job.html")

    customer_name = (request.form.get("customer_name") or "").strip()
    customer_email = (request.form.get("customer_email") or "").strip() or None
    customer_phone = (request.form.get("customer_phone") or "").strip() or None
    job_ref = (request.form.get("job_ref") or "").strip() or None
    part_name = (request.form.get("part_name") or "").strip()
    distributor = (request.form.get("distributor") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None
    eta_date = (request.form.get("eta_date") or "").strip() or None

    errors: list[str] = []
    if not customer_name:
        errors.append("Customer name is required.")
    if not part_name:
        errors.append("Part name is required.")
    if eta_date:
        try:
            from datetime import datetime

            datetime.strptime(eta_date, "%Y-%m-%d")
        except ValueError:
            errors.append("ETA must be YYYY-MM-DD.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("new_job.html", form=request.form), 400

    token = secrets.token_hex(32)
    now = H.utc_now_iso()
    db = H.get_db()
    cur = db.execute(
        """
        INSERT INTO jobs (
            token, customer_name, customer_email, customer_phone, job_ref,
            part_name, distributor, notes, eta_date, milestone,
            created_at, updated_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ordered', ?, ?, NULL)
        """,
        (
            token,
            customer_name,
            customer_email,
            customer_phone,
            job_ref,
            part_name,
            distributor,
            notes,
            eta_date,
            now,
            now,
        ),
    )
    job_id = cur.lastrowid
    H.add_event(db, job_id, "created", milestone="ordered")
    db.commit()
    flash("Parts job created.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<int:job_id>")
def job_detail(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    return render_template(
        "job_detail.html",
        job=job,
        events=H.list_events(job_id),
        public_url=H.public_job_url(job["token"]),
        sms_text=H.sms_blurb(job),
        email_text=H.email_blurb(job),
        email_subject=H.email_subject(job),
        can_notify=H.has_notify_destination(job),
        can_email=H.can_notify_email(job),
        can_sms=H.can_notify_sms(job),
        notify_default=H.has_notify_destination(job),
    )


@app.post("/jobs/<int:job_id>/milestone")
def set_milestone(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)

    new_milestone = (request.form.get("milestone") or "").strip()
    note = (request.form.get("note") or "").strip() or None
    eta_raw = (request.form.get("eta_date") or "").strip()
    eta_submitted = "eta_date" in request.form
    new_eta = eta_raw or None if eta_submitted else job["eta_date"]
    if eta_raw:
        try:
            from datetime import datetime

            datetime.strptime(eta_raw, "%Y-%m-%d")
        except ValueError:
            flash("ETA must be YYYY-MM-DD.", "error")
            return redirect(url_for("job_detail", job_id=job_id))

    notify_flag = request.form.get("notify") == "1"

    if new_milestone not in H.MILESTONES:
        flash("Unknown milestone.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    same_milestone = new_milestone == job["milestone"]
    eta_changed = (new_eta or None) != (job["eta_date"] or None)
    note_provided = bool(note)

    if same_milestone and not note_provided and not eta_changed:
        flash("No change (same milestone).", "ok")
        return redirect(url_for("job_detail", job_id=job_id))

    now = H.utc_now_iso()
    closed_at = job["closed_at"]
    if new_milestone in H.TERMINAL_MILESTONES:
        closed_at = now
    elif job["milestone"] in H.TERMINAL_MILESTONES and new_milestone not in H.TERMINAL_MILESTONES:
        closed_at = None

    db = H.get_db()
    db.execute(
        """
        UPDATE jobs SET milestone = ?, eta_date = ?, updated_at = ?, closed_at = ?
        WHERE id = ?
        """,
        (new_milestone, new_eta, now, closed_at, job_id),
    )

    if not same_milestone:
        H.add_event(
            db,
            job_id,
            "milestone",
            milestone=new_milestone,
            note=note,
            meta={"eta_date": new_eta} if eta_changed else None,
        )
    else:
        if eta_changed:
            H.add_event(
                db,
                job_id,
                "eta",
                milestone=new_milestone,
                note=f"ETA set to {new_eta}" if new_eta else "ETA cleared",
                meta={"eta_date": new_eta},
            )
        if note_provided:
            H.add_event(db, job_id, "note", milestone=new_milestone, note=note)

    db.commit()

    job = H.get_job(job_id)
    notify_failed = False
    if notify_flag and H.has_notify_destination(job):
        attempted, ok, _errs = H.notify_customer(app, job)
        db.commit()
        if attempted and not ok:
            notify_failed = True

    if notify_failed:
        flash("Saved; notify failed.", "error")
    else:
        flash("Milestone updated.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/paste")
def paste_distributor(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    raw = (request.form.get("raw_paste") or "").strip()
    if not raw:
        flash("Paste text is empty.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    now = H.utc_now_iso()
    db = H.get_db()
    H.add_event(
        db,
        job_id,
        "paste",
        milestone=job["milestone"],
        raw_paste=raw,
        note="Distributor paste",
    )
    db.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, job_id))
    db.commit()
    flash("Distributor note saved (not auto-parsed).", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/notify")
def notify_now(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    if not H.has_notify_destination(job):
        flash("No notify channel configured for this customer.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    attempted, ok, _errs = H.notify_customer(app, job)
    H.get_db().commit()
    if attempted and not ok:
        flash("Saved; notify failed.", "error")
    elif attempted:
        flash("Customer notified.", "ok")
    else:
        flash("Nothing to send.", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/p/<token>")
def public_job(token: str):
    job = H.get_job_by_token(token)
    if job is None:
        abort(404)
    events = H.list_events(job["id"])
    public_events = [
        e for e in events if e["kind"] in ("created", "milestone", "eta", "note")
    ]
    return render_template(
        "public_job.html",
        job=job,
        events=public_events,
        public=True,
    )


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


get_job = H.get_job
get_job_by_token = H.get_job_by_token
get_db = H.get_db
list_events = H.list_events

init_db()


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
