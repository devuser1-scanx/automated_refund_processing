"""automated_refund_processing worker.

Run every 5 minutes (Cloud Scheduler -> Cloud Run Job). Each run:
  1. Releases rows stuck in 'processing' from a crashed run.
  2. Claims due 'pending' rows from refund_queue (FOR UPDATE SKIP LOCKED, so
     overlapping runs never take the same row).
  3. For each row: re-checks the appointment, looks up its Stripe payments in
     Acuity, and issues a full Stripe refund for each one.
  4. Sends email + Google Chat alerts for succeeded / failed / needs_review
     rows not yet notified (see notify.py). A channel that is down is retried
     on the next run.

Environment:
  DATABASE_URL        postgres connection string (Cloud SQL socket is fine)
  STRIPE_SECRET_KEY   key for the Stripe account connected to Acuity
  ACUITY_USER_ID      Acuity API user id
  ACUITY_API_KEY      Acuity API key
  BATCH_SIZE          rows per run (default 20)
  MAX_ATTEMPTS        retries for transient errors before 'failed' (default 5)
  Alert settings (NOTIFY_EMAILS, GMAIL_*, GCHAT_WEBHOOK_URL, DISPLAY_TIMEZONE): see notify.py
"""

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import psycopg
import requests
import stripe
from psycopg.rows import dict_row

from automated_refund_processing import notify

log = logging.getLogger("automated_refund_processing")

ACUITY_BASE = "https://acuityscheduling.com/api/v1"
STALE_PROCESSING = "15 minutes"


class TransientError(Exception):
    """Worth retrying later (network, rate limit, 5xx)."""


class NeedsReview(Exception):
    """A person has to look at this one; never retried automatically."""


@dataclass
class Payment:
    transaction_id: str
    amount: Decimal


# --- Acuity -----------------------------------------------------------------

def acuity_stripe_payments(appointment_id: str) -> list[Payment]:
    """Stripe payments Acuity recorded for an appointment."""
    try:
        resp = requests.get(
            f"{ACUITY_BASE}/appointments/{appointment_id}/payments",
            auth=(os.environ["ACUITY_USER_ID"], os.environ["ACUITY_API_KEY"]),
            timeout=15,
        )
    except requests.RequestException as e:
        raise TransientError(f"acuity request failed: {e}") from e

    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientError(f"acuity {resp.status_code}: {resp.text[:200]}")
    if resp.status_code == 404:
        raise NeedsReview(f"acuity has no appointment {appointment_id}")
    if resp.status_code != 200:
        raise NeedsReview(f"acuity {resp.status_code}: {resp.text[:200]}")

    payments = []
    for p in resp.json():
        txn = (p.get("transactionID") or "").strip()
        amount = Decimal(str(p.get("amount") or "0"))
        is_stripe = "stripe" in (p.get("processor") or "").lower() or txn.startswith(("ch_", "pi_", "py_"))
        if is_stripe and txn and amount > 0:
            payments.append(Payment(txn, amount))
    return payments


# --- Stripe -----------------------------------------------------------------

def stripe_full_refund(txn: str, appointment_id: str, queue_id: int) -> str:
    """Full refund of one charge / payment intent. Returns the refund id."""
    params = {"metadata": {"appointment_id": appointment_id, "refund_queue_id": str(queue_id)}}
    if txn.startswith("pi_"):
        params["payment_intent"] = txn
    else:
        params["charge"] = txn

    try:
        refund = stripe.Refund.create(
            api_key=os.environ["STRIPE_SECRET_KEY"],
            # Same key on retry -> Stripe returns the original refund, never a second one.
            idempotency_key=f"automated_refund_processing-{appointment_id}-{txn}",
            **params,
        )
    except stripe.InvalidRequestError as e:
        if e.code == "charge_already_refunded":
            return "already_refunded"
        raise NeedsReview(f"stripe rejected refund for {txn}: {e.user_message or e}") from e
    except (stripe.APIConnectionError, stripe.RateLimitError) as e:
        raise TransientError(f"stripe: {e}") from e
    except stripe.APIError as e:
        raise TransientError(f"stripe api error: {e}") from e

    if refund.status in ("failed", "canceled"):
        raise NeedsReview(f"stripe refund {refund.id} for {txn} is {refund.status}")
    return refund.id


# --- Queue ------------------------------------------------------------------

def release_stale(conn) -> None:
    conn.execute(
        f"""UPDATE refund_queue
               SET status = 'pending', locked_at = NULL, updated_at = now()
             WHERE status = 'processing'
               AND locked_at < now() - interval '{STALE_PROCESSING}'"""
    )


def claim(conn, batch_size: int) -> list[dict]:
    return conn.execute(
        """UPDATE refund_queue q
              SET status = 'processing', attempts = attempts + 1,
                  locked_at = now(), updated_at = now()
            WHERE q.id IN (
                  SELECT r.id
                    FROM refund_queue r
                    JOIN appointment a USING (appointment_id)
                   WHERE r.status = 'pending'
                     AND r.due_at <= now()
                     AND is_refundable_status(a.status_label)
                     AND a.paid
                   ORDER BY r.due_at
                   LIMIT %s
                     FOR UPDATE OF r SKIP LOCKED)
        RETURNING q.*""",
        (batch_size,),
    ).fetchall()


def still_refundable(conn, appointment_id: str) -> bool:
    row = conn.execute(
        "SELECT is_refundable_status(status_label) AND coalesce(paid, false) "
        "       AND NOT is_refund_excluded_type(appointment_type) AS ok "
        "FROM appointment WHERE appointment_id = %s",
        (appointment_id,),
    ).fetchone()
    return bool(row and row["ok"])


def finish(conn, queue_id: int, status: str, *, error=None, payment=None, refund=None, refunded=None) -> None:
    conn.execute(
        """UPDATE refund_queue
              SET status = %s, last_error = %s,
                  stripe_payment_intent = coalesce(%s, stripe_payment_intent),
                  stripe_refund_id = coalesce(%s, stripe_refund_id),
                  amount_refunded = coalesce(%s, amount_refunded),
                  locked_at = NULL, updated_at = now()
            WHERE id = %s""",
        (status, error, payment, refund, refunded, queue_id),
    )


def retry_later(conn, row: dict, error: str, max_attempts: int) -> None:
    if row["attempts"] >= max_attempts:
        finish(conn, row["id"], "failed", error=error)
        return
    backoff_minutes = 2 ** row["attempts"]  # 2, 4, 8, 16 ...
    conn.execute(
        """UPDATE refund_queue
              SET status = 'pending', last_error = %s, locked_at = NULL,
                  due_at = now() + make_interval(mins => %s), updated_at = now()
            WHERE id = %s""",
        (error, backoff_minutes, row["id"]),
    )


# --- Main -------------------------------------------------------------------

def process(conn, row: dict, max_attempts: int) -> None:
    appt = row["appointment_id"]

    # The claim already checked this, but a status change can land between
    # the claim and now. Last line of defence before money moves.
    if not still_refundable(conn, appt):
        finish(conn, row["id"], "cancelled", error="appointment no longer cancelled/no-show, unpaid, or type excluded")
        return

    try:
        payments = acuity_stripe_payments(appt)
        if not payments:
            raise NeedsReview("no Stripe payment recorded in Acuity")

        refund_ids = [stripe_full_refund(p.transaction_id, appt, row["id"]) for p in payments]
    except TransientError as e:
        log.warning("appointment %s: transient error, will retry: %s", appt, e)
        retry_later(conn, row, str(e), max_attempts)
        return
    except NeedsReview as e:
        log.error("appointment %s: needs review: %s", appt, e)
        finish(conn, row["id"], "needs_review", error=str(e))
        return

    note = None
    total = sum(p.amount for p in payments)
    if row["amount"] is not None and total != row["amount"]:
        note = f"refunded {total} but amount_paid was {row['amount']}"
    finish(
        conn, row["id"], "succeeded", error=note,
        payment=",".join(p.transaction_id for p in payments),
        refund=",".join(refund_ids),
        refunded=total,
    )
    log.info("appointment %s: refunded %s (%s)", appt, total, ",".join(refund_ids))


def run_once() -> int:
    batch_size = int(os.environ.get("BATCH_SIZE", "20"))
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "5"))

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        release_stale(conn)
        with conn.transaction():
            rows = claim(conn, batch_size)
        for row in rows:
            try:
                process(conn, row, max_attempts)
            except Exception as e:  # never let one row stop the batch
                log.exception("appointment %s: unexpected error", row["appointment_id"])
                retry_later(conn, row, f"unexpected: {e}", max_attempts)

        # Also picks up alerts a previous run couldn't deliver.
        try:
            notify.send_pending(conn)
        except Exception:
            log.exception("sending alerts failed; will retry next run")
    return len(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = run_once()
    log.info("processed %d refund(s)", n)
