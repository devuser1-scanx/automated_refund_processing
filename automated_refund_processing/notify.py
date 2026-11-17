"""Email + Google Chat alerts for finished refunds.

One alert per refund_queue row once it is succeeded, failed or needs_review.
Each channel is tracked separately (email_notified_at, gchat_notified_at), so
if Google Chat is down the email isn't sent twice when the alert is retried on
the next run. A row stops being retried after MAX_NOTIFY_ATTEMPTS.

Email goes through the Gmail API as a Google Workspace mailbox, using a service
account with domain-wide delegation (gmail.send scope), the same setup as the
other ScanX backend services. No SMTP.

Environment (a channel with no settings is skipped):
  NOTIFY_EMAILS               comma-separated recipients
  GMAIL_SENDER_EMAIL          Workspace mailbox to send as (existing secret)
  GMAIL_SERVICE_ACCOUNT_JSON  service-account key JSON, one line (existing secret)
  GCHAT_WEBHOOK_URL   incoming webhook URL of the space (secret: keep in Secret Manager)
  DISPLAY_TIMEZONE    timezone for dates in alerts (default America/Chicago)
"""

import base64
import html
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

log = logging.getLogger("automated_refund_processing.notify")

MAX_NOTIFY_ATTEMPTS = 5

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SENDER_DISPLAY_NAME = "ScanX Health LLC"

HEADLINES = {
    "succeeded": ("Refund Succeeded", "✅"),
    "failed": ("Refund Failed", "❌"),
    "needs_review": ("Refund Needs Review", "⚠️"),
}

# Every refund_queue column shown in the "Queue record" part of an alert.
QUEUE_FIELDS = [
    ("Queue ID", "id"),
    ("Appointment ID", "appointment_id"),
    ("Queue Status", "status"),
    ("Trigger Status", "trigger_status"),
    ("Amount Paid", "amount"),
    ("Amount Refunded", "amount_refunded"),
    ("Stripe Payment", "stripe_payment_intent"),
    ("Stripe Refund", "stripe_refund_id"),
    ("Attempts", "attempts"),
    ("Due At", "due_at"),
    ("Locked At", "locked_at"),
    ("Last Error", "last_error"),
    ("Created At", "created_at"),
    ("Updated At", "updated_at"),
]


# --- Formatting -------------------------------------------------------------

def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "America/Chicago"))


def money(v) -> str:
    return "" if v is None else f"${Decimal(v):,.2f}"


def when(v) -> str:
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone(_tz())
        return v.strftime("%b %d, %Y %I:%M %p %Z").strip()
    return "" if v is None else str(v)


def reason(trigger_status: str) -> str:
    key = "".join(c for c in (trigger_status or "").lower() if c.isalpha())
    return "No-show" if key == "noshow" else "Cancelled"


def appointment_time(row: dict) -> tuple[str, str, str]:
    """(date, day, time) in DISPLAY_TIMEZONE, e.g. ('November 17, 2026', 'Tuesday', '8:45 AM CST')."""
    dt = row.get("appointment_datetime")
    if dt is not None:
        dt = dt.astimezone(_tz())
        return (f"{dt:%B} {dt.day}, {dt.year}", f"{dt:%A}",
                f"{dt:%I:%M %p %Z}".lstrip("0"))
    d = row.get("appt_date")  # fall back to the date / time columns
    if isinstance(d, (datetime, date)):
        return (f"{d:%B} {d.day}, {d.year}", f"{d:%A}", row.get("appt_time") or "")
    return ("", "", row.get("appt_time") or "")


def details(row: dict) -> list[tuple[str, str]]:
    """Patient + refund lines, in display order. Empty values are left out."""
    d, day, t = appointment_time(row)
    name = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)
    refunded = money(row["amount_refunded"]) if row["status"] == "succeeded" else "$0.00 (not refunded)"
    lines = [
        ("Patient Name", name),
        ("Appointment ID", row["appointment_id"]),
        ("Exam Type", row.get("appointment_type")),
        ("Clinic", row.get("location")),
        ("Appointment Date", d),
        ("Day", day),
        ("Time", t),
        ("Reason", reason(row["trigger_status"])),
        ("Amount Refunded", refunded),
        ("Phone", row.get("phone")),
        ("Email", row.get("patient_email")),
    ]
    if row["status"] != "succeeded":
        lines.append(("Problem", row.get("last_error")))
    return [(k, str(v)) for k, v in lines if v not in (None, "")]


def queue_record(row: dict) -> list[tuple[str, str]]:
    out = []
    for label, col in QUEUE_FIELDS:
        v = row.get(col)
        if col in ("amount", "amount_refunded"):
            v = money(v)
        elif isinstance(v, datetime):
            v = when(v)
        out.append((label, "" if v is None else str(v)))
    return out


def stripe_link(row: dict) -> str | None:
    refund = (row.get("stripe_refund_id") or "").split(",")[0]
    if not refund.startswith("re_"):
        return None
    test = os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test")
    return f"https://dashboard.stripe.com/{'test/' if test else ''}refunds/{refund}"


# --- Google Chat ------------------------------------------------------------

def gchat_card(row: dict) -> dict:
    title, icon = HEADLINES[row["status"]]
    para = lambda k, v: {"textParagraph": {"text": f"<b>{html.escape(k)}:</b> {html.escape(v)}"}}
    buttons = []
    if row.get("phone"):
        buttons.append({"text": "Call Patient", "icon": {"knownIcon": "PHONE"},
                        "onClick": {"openLink": {"url": f"tel:{row['phone']}"}}})
    if link := stripe_link(row):
        buttons.append({"text": "Open in Stripe", "onClick": {"openLink": {"url": link}}})

    sections = [
        {"widgets": [para(k, v) for k, v in details(row)]},
        {"header": "Queue record", "collapsible": True, "uncollapsibleWidgetsCount": 0,
         "widgets": [para(k, v or "-") for k, v in queue_record(row)]},
    ]
    if buttons:
        sections.append({"widgets": [{"buttonList": {"buttons": buttons}}]})
    return {"cardsV2": [{
        "cardId": f"refund-{row['id']}",
        "card": {"header": {"title": f"{title} {icon}", "subtitle": "Patient details below"},
                 "sections": sections},
    }]}


def _post_gchat(payload: dict) -> None:
    resp = requests.post(os.environ["GCHAT_WEBHOOK_URL"], json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"google chat {resp.status_code}: {resp.text[:200]}")


# --- Email ------------------------------------------------------------------

def email_message(row: dict) -> EmailMessage:
    title, icon = HEADLINES[row["status"]]
    info = dict(details(row))
    subject = f"{icon} {title}: {info.get('Patient Name', row['appointment_id'])}"
    if row["status"] == "succeeded":
        subject += f" · {info['Amount Refunded']}"
    if "Appointment Date" in info:
        subject += f" · {info['Appointment Date']}"

    def table(rows):
        return "".join(
            f"<tr><td style='padding:4px 12px 4px 0;color:#555;white-space:nowrap'><b>{html.escape(k)}</b></td>"
            f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>" for k, v in rows)

    colour = {"succeeded": "#1e8e3e", "failed": "#d93025", "needs_review": "#e37400"}[row["status"]]
    link = stripe_link(row)
    body = (
        "<div style='font-family:Arial,sans-serif;font-size:14px;max-width:600px'>"
        f"<h2 style='color:{colour};margin:0 0 4px'>{icon} {html.escape(title)}</h2>"
        "<p style='color:#555;margin:0 0 16px'>Patient details below</p>"
        f"<table style='border-collapse:collapse'>{table(details(row))}</table>"
        + (f"<p><a href='{html.escape(link)}'>Open refund in Stripe</a></p>" if link else "")
        + "<h3 style='margin:24px 0 8px;font-size:14px'>Queue record</h3>"
        f"<table style='border-collapse:collapse;font-size:13px'>{table((k, v or '-') for k, v in queue_record(row))}</table>"
        "<p style='color:#888;font-size:12px;margin-top:24px'>Sent by automated_refund_processing</p></div>"
    )
    text = "\n".join(f"{k}: {v}" for k, v in details(row)) + "\n\nQueue record\n" + \
        "\n".join(f"{k}: {v or '-'}" for k, v in queue_record(row))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((GMAIL_SENDER_DISPLAY_NAME, os.environ["GMAIL_SENDER_EMAIL"]))
    msg["To"] = ", ".join(_recipients())
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


def _recipients() -> list[str]:
    return [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()]


def _gmail_token() -> str:
    """Short-lived access token for GMAIL_SENDER_EMAIL via domain-wide delegation."""
    try:
        info = json.loads(os.environ["GMAIL_SERVICE_ACCOUNT_JSON"])
    except json.JSONDecodeError as e:
        raise RuntimeError("GMAIL_SERVICE_ACCOUNT_JSON is not valid JSON") from e
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[GMAIL_SEND_SCOPE]
    ).with_subject(os.environ["GMAIL_SENDER_EMAIL"])
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("could not obtain a Gmail access token")
    return creds.token


def _send_email(msg: EmailMessage) -> None:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    resp = requests.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {_gmail_token()}"},
        json={"raw": raw},
        timeout=60,
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"gmail {resp.status_code}: {detail[:200]}")


# --- Sending ----------------------------------------------------------------

def email_enabled() -> bool:
    return bool(_recipients() and os.environ.get("GMAIL_SENDER_EMAIL")
                and os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON"))


def gchat_enabled() -> bool:
    return bool(os.environ.get("GCHAT_WEBHOOK_URL"))


def send_pending(conn, limit: int = 50) -> int:
    """Send every alert still owed. Returns how many rows were looked at."""
    email_on, gchat_on = email_enabled(), gchat_enabled()
    if not (email_on or gchat_on):
        return 0

    with conn.transaction():
        # Row locks are held while sending, so an overlapping run skips these rows.
        rows = conn.execute(
            """SELECT q.*, a.first_name, a.last_name, a.phone, a.email AS patient_email,
                      a.appointment_type, a.location, a.appointment_datetime,
                      a.date AS appt_date, a.time AS appt_time
                 FROM refund_queue q
                 JOIN appointment a USING (appointment_id)
                WHERE q.status IN ('succeeded', 'failed', 'needs_review')
                  AND q.notify_attempts < %(max)s
                  AND ((%(email)s AND q.email_notified_at IS NULL)
                    OR (%(gchat)s AND q.gchat_notified_at IS NULL))
                ORDER BY q.id
                LIMIT %(limit)s
                  FOR UPDATE OF q SKIP LOCKED""",
            {"max": MAX_NOTIFY_ATTEMPTS, "email": email_on, "gchat": gchat_on, "limit": limit},
        ).fetchall()

        for row in rows:
            errors, sent_email, sent_gchat = [], False, False
            if email_on and row["email_notified_at"] is None:
                try:
                    _send_email(email_message(row))
                    sent_email = True
                except Exception as e:
                    errors.append(f"email: {e}")
            if gchat_on and row["gchat_notified_at"] is None:
                try:
                    _post_gchat(gchat_card(row))
                    sent_gchat = True
                except Exception as e:
                    errors.append(f"gchat: {e}")

            conn.execute(
                """UPDATE refund_queue
                      SET email_notified_at = CASE WHEN %s THEN now() ELSE email_notified_at END,
                          gchat_notified_at = CASE WHEN %s THEN now() ELSE gchat_notified_at END,
                          notify_attempts = notify_attempts + CASE WHEN %s THEN 1 ELSE 0 END,
                          notify_error = %s
                    WHERE id = %s""",
                (sent_email, sent_gchat, bool(errors), "; ".join(errors) or None, row["id"]),
            )
            if errors:
                log.warning("queue %s: alert not delivered, will retry: %s", row["id"], "; ".join(errors))
    return len(rows)
