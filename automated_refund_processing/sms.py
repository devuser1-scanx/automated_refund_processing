"""Text message to the patient after a refund succeeds (Twilio).

One text per refund_queue row once it is 'succeeded'. Tracked in sms_notified_at /
sms_attempts / sms_error, separately from the staff alerts in notify.py, so a
Twilio problem never affects refunds or the email / Google Chat alerts.

Refunds that were already done by hand before the worker ran
(stripe_refund_id = 'already_refunded') are not texted: ScanX did not issue them.

Environment (SMS is skipped entirely unless all four are set):
  TWILIO_ACCOUNT_SID   Twilio account SID (secret)
  TWILIO_AUTH_TOKEN    Twilio auth token (secret)
  TWILIO_FROM_NUMBER   sending number in E.164, e.g. +12145550100
                       (or TWILIO_MESSAGING_SERVICE_SID instead, starts with MG)
  CLINIC_PHONE         number patients are told to call, as it should read in the text
  DISPLAY_TIMEZONE     timezone for the appointment date (default America/Chicago)
"""

import logging
import os
import re

import requests

from automated_refund_processing.notify import MAX_NOTIFY_ATTEMPTS, appointment_time, money, reason

log = logging.getLogger("automated_refund_processing.sms")

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

MESSAGE = (
    "ScanX Health: Your refund of {amount} for your {when} appointment, which was {reason}, "
    "has been issued. For questions, please call {clinic_phone}."
)
# Added only when the appointment has a patient email on file.
RECEIPT_SENTENCE = " Receipt of this transaction is sent to you via your registered email account."


class PermanentError(Exception):
    """Retrying will not help (no / invalid phone number, Twilio rejected the number)."""


def to_e164(raw) -> str:
    """'(214) 555-0100', '214-555-0100', '1 214 555 0100', '+1214...' -> '+12145550100'.

    Numbers without a country code are treated as US. Raises PermanentError if it
    does not look like a phone number.
    """
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        ok = 8 <= len(digits) <= 15
        number = "+" + digits
    elif len(digits) == 10:
        ok, number = True, "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        ok, number = True, "+" + digits
    else:
        ok, number = False, ""
    if not ok:
        raise PermanentError(f"no usable phone number on the appointment ({raw!r})")
    return number


def reason_text(trigger_status: str) -> str:
    """'cancelled' or 'a no-show', for 'which was ...'."""
    return "a no-show" if reason(trigger_status) == "No-show" else "cancelled"


def message_text(row: dict) -> str:
    appt_date, _day, appt_time = appointment_time(row)
    when = f"{appt_date} at {appt_time}" if appt_date and appt_time else (appt_date or "recent")
    text = MESSAGE.format(
        amount=money(row["amount_refunded"]),
        when=when,
        reason=reason_text(row["trigger_status"]),
        clinic_phone=_env("CLINIC_PHONE"),
    )
    if (row.get("patient_email") or "").strip():
        text += RECEIPT_SENTENCE
    return text


def _env(name: str) -> str:
    """Setting value without stray spaces / newlines (secrets made in the console often have one)."""
    return os.environ.get(name, "").strip()


def enabled() -> bool:
    return bool(_env("TWILIO_ACCOUNT_SID") and _env("TWILIO_AUTH_TOKEN")
                and (_env("TWILIO_FROM_NUMBER") or _env("TWILIO_MESSAGING_SERVICE_SID"))
                and _env("CLINIC_PHONE"))


def _send_sms(to: str, body: str) -> None:
    data = {"To": to, "Body": body}
    if _env("TWILIO_MESSAGING_SERVICE_SID"):
        data["MessagingServiceSid"] = _env("TWILIO_MESSAGING_SERVICE_SID")
    else:
        data["From"] = _env("TWILIO_FROM_NUMBER")
    sid = _env("TWILIO_ACCOUNT_SID")
    resp = requests.post(
        TWILIO_URL.format(sid=sid), data=data,
        auth=(sid, _env("TWILIO_AUTH_TOKEN")), timeout=15,
    )
    if resp.status_code < 400:
        return
    try:
        err = resp.json()
        detail = f"{err.get('code')}: {err.get('message')}"
    except ValueError:
        detail = resp.text[:200]
    # 4xx other than rate limiting (429) means the request itself is wrong
    # (bad number, unsubscribed, bad credentials): do not keep retrying.
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        raise PermanentError(f"twilio {resp.status_code} {detail[:200]}")
    raise RuntimeError(f"twilio {resp.status_code} {detail[:200]}")


def send_pending(conn, limit: int = 50) -> int:
    """Text every patient whose refund succeeded and who has not been texted yet."""
    if not enabled():
        return 0

    with conn.transaction():
        # Row locks are held while sending, so an overlapping run skips these rows.
        rows = conn.execute(
            """SELECT q.*, a.phone, a.email AS patient_email, a.appointment_datetime,
                      a.date AS appt_date, a.time AS appt_time
                 FROM refund_queue q
                 JOIN appointment a USING (appointment_id)
                WHERE q.status = 'succeeded'
                  AND q.sms_notified_at IS NULL
                  AND q.sms_attempts < %(max)s
                  AND coalesce(q.stripe_refund_id, '') <> 'already_refunded'
                  AND coalesce(q.amount_refunded, 0) > 0
                ORDER BY q.id
                LIMIT %(limit)s
                  FOR UPDATE OF q SKIP LOCKED""",
            {"max": MAX_NOTIFY_ATTEMPTS, "limit": limit},
        ).fetchall()

        for row in rows:
            sent, error, give_up = False, None, False
            try:
                _send_sms(to_e164(row["phone"]), message_text(row))
                sent = True
            except PermanentError as e:
                error, give_up = str(e), True
            except Exception as e:
                error = str(e)

            conn.execute(
                """UPDATE refund_queue
                      SET sms_notified_at = CASE WHEN %s THEN now() ELSE sms_notified_at END,
                          sms_attempts = CASE WHEN %s THEN %s ELSE sms_attempts + CASE WHEN %s THEN 0 ELSE 1 END END,
                          sms_error = %s
                    WHERE id = %s""",
                (sent, give_up, MAX_NOTIFY_ATTEMPTS, sent, error, row["id"]),
            )
            if error:
                log.warning("queue %s: patient SMS not sent%s: %s", row["id"],
                            "" if give_up else ", will retry", error)
    return len(rows)
