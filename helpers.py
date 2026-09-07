"""PartPing helpers: env, db, mail, SMS, blurbs, milestones."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import g

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "partping.db"

OPEN_ENDPOINTS = {
    "health",
    "login",
    "logout",
    "public_job",
    "static",
}

MILESTONES = (
    "ordered",
    "shipped",
    "arrived",
    "ready",
    "closed",
    "cancelled",
)

MILESTONE_LABELS = {
    "ordered": "Part ordered",
    "shipped": "Part shipped / in transit",
    "arrived": "Part in house / received",
    "ready": "Ready to schedule / install",
    "closed": "Job moved on",
    "cancelled": "Cancelled",
}

TERMINAL_MILESTONES = frozenset({"closed", "cancelled"})

MILESTONE_ORDER = {m: i for i, m in enumerate(MILESTONES)}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    return raw if raw else str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def business_name() -> str:
    return _env("BUSINESS_NAME") or "PartPing"


def business_phone() -> str:
    return _env("BUSINESS_PHONE")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def public_job_url(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/p/{token}"


def milestone_label(key: str) -> str:
    return MILESTONE_LABELS.get(key, key)


def format_eta_display(eta_date: str | None) -> str:
    if not eta_date:
        return "ETA not set yet"
    try:
        dt = datetime.strptime(eta_date, "%Y-%m-%d")
        return f"ETA: {dt.strftime('%b')} {dt.day}, {dt.year}"
    except ValueError:
        return f"ETA: {eta_date}"


def format_eta_short(eta_date: str | None) -> str:
    if not eta_date:
        return "ETA not set yet"
    try:
        dt = datetime.strptime(eta_date, "%Y-%m-%d")
        return f"ETA: {dt.strftime('%b')} {dt.day}"
    except ValueError:
        return f"ETA: {eta_date}"


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


def close_db(_exc: BaseException | None = None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            customer_phone TEXT,
            job_ref TEXT,
            part_name TEXT NOT NULL,
            distributor TEXT,
            notes TEXT,
            eta_date TEXT,
            milestone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            kind TEXT NOT NULL,
            milestone TEXT,
            note TEXT,
            raw_paste TEXT,
            at TEXT NOT NULL,
            meta_json TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_token ON jobs(token);
        CREATE INDEX IF NOT EXISTS idx_jobs_milestone ON jobs(milestone);
        CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
        """
    )
    db.commit()


def get_job(job_id: int):
    return get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_token(token: str):
    return get_db().execute("SELECT * FROM jobs WHERE token = ?", (token,)).fetchone()


def list_events(job_id: int):
    return get_db().execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY id ASC", (job_id,)
    ).fetchall()


def add_event(
    conn,
    job_id: int,
    kind: str,
    *,
    milestone: str | None = None,
    note: str | None = None,
    raw_paste: str | None = None,
    meta=None,
    at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events (job_id, kind, milestone, note, raw_paste, at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            kind,
            milestone,
            note,
            raw_paste,
            at or utc_now_iso(),
            json.dumps(meta) if meta is not None else None,
        ),
    )


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def send_twilio_sms(to_phone: str, body: str) -> None:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise RuntimeError("Twilio is not configured.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode(
        {"To": to_phone, "From": from_number, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Twilio HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Twilio HTTP {exc.code}") from exc


def sms_blurb(job) -> str:
    business = business_name()
    link = public_job_url(job["token"])
    label = milestone_label(job["milestone"])
    eta = format_eta_short(job["eta_date"])
    return (
        f"Hi {job['customer_name']}, update from {business} on your {job['part_name']}: "
        f"{label}. {eta}. Track here: {link}"
    )


def email_blurb(job) -> str:
    business = business_name()
    link = public_job_url(job["token"])
    label = milestone_label(job["milestone"])
    eta_line = format_eta_display(job["eta_date"])
    sign_name = _env("FROM_NAME") or business
    body = (
        f"Hi {job['customer_name']},\n\n"
        f"Update on your {job['part_name']} order from {business}.\n\n"
        f"Status: {label}\n"
        f"{eta_line}\n"
    )
    if job["job_ref"]:
        body += f"Job / WO: {job['job_ref']}\n"
    body += f"\nTrack your part here:\n{link}\n\nThank you,\n{sign_name}\n"
    return body


def email_subject(job) -> str:
    return f"Update on your {job['part_name']} order"


def notify_update_body(job) -> str:
    """Short body used for SMTP notify on milestone change."""
    link = public_job_url(job["token"])
    label = milestone_label(job["milestone"])
    eta_line = format_eta_display(job["eta_date"])
    business = business_name()
    lines = [
        f"Hi {job['customer_name']},",
        "",
        f"Update on your {job['part_name']} order from {business}.",
        "",
        f"Status: {label}",
        eta_line,
        "",
        f"Track here: {link}",
        "",
        f"- {business}",
    ]
    return "\n".join(lines)


def notify_sms_body(job) -> str:
    link = public_job_url(job["token"])
    label = milestone_label(job["milestone"])
    return f"{business_name()}: {job['part_name']} - {label}. {link}"


def can_notify_email(job) -> bool:
    return smtp_configured() and bool((job["customer_email"] or "").strip())


def can_notify_sms(job) -> bool:
    return sms_configured() and bool((job["customer_phone"] or "").strip())


def has_notify_destination(job) -> bool:
    return can_notify_email(job) or can_notify_sms(job)


def notify_customer(app, job) -> tuple[bool, bool, list[str]]:
    """Send email and/or SMS. Returns (any_attempted, all_ok, channel_errors)."""
    errors: list[str] = []
    attempted = False
    db = get_db()

    if can_notify_email(job):
        attempted = True
        to_email = (job["customer_email"] or "").strip()
        try:
            send_smtp(to_email, email_subject(job), notify_update_body(job))
            add_event(db, job["id"], "notify_email", meta={"to": to_email, "ok": True})
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "Email notify failed for job_id=%s: %s", job["id"], type(exc).__name__
            )
            add_event(
                db, job["id"], "notify_email", meta={"to": to_email, "ok": False}
            )
            errors.append("email")

    if can_notify_sms(job):
        attempted = True
        to_phone = (job["customer_phone"] or "").strip()
        try:
            send_twilio_sms(to_phone, notify_sms_body(job))
            add_event(db, job["id"], "notify_sms", meta={"to": to_phone, "ok": True})
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "SMS notify failed for job_id=%s: %s", job["id"], type(exc).__name__
            )
            add_event(db, job["id"], "notify_sms", meta={"to": to_phone, "ok": False})
            errors.append("sms")

    owner_to = _env("OWNER_NOTIFY_EMAIL")
    if smtp_configured() and owner_to:
        try:
            subject = f"PartPing update: {job['customer_name']} - {job['part_name']}"
            base = public_base_url() or "http://localhost:8080"
            body = (
                f"Milestone: {milestone_label(job['milestone'])}\n"
                f"Customer: {job['customer_name']}\n"
                f"Part: {job['part_name']}\n"
                f"{format_eta_display(job['eta_date'])}\n\n"
                f"Open in PartPing: {base}/jobs/{job['id']}\n"
            )
            send_smtp(owner_to, subject, body)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "Owner notify failed for job_id=%s: %s", job["id"], type(exc).__name__
            )

    return attempted, (not errors) if attempted else True, errors
