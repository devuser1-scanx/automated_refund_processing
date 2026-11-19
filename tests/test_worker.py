"""Runs the worker against a real Postgres with Acuity, Stripe, email and Google Chat faked.

TEST_DATABASE_URL must point at a scratch database; tables are recreated.
"""

import os
import pathlib
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
import stripe

from automated_refund_processing import notify, worker

DB = os.environ.get("TEST_DATABASE_URL")
MIGRATIONS = sorted((pathlib.Path(__file__).parent.parent / "migrations").glob("001_*.sql"))
pytestmark = pytest.mark.skipif(not DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("ACUITY_USER_ID", "u")
    monkeypatch.setenv("ACUITY_API_KEY", "k")
    monkeypatch.setenv("NOTIFY_EMAILS", "devuser1@scanx.care, clinic1@scanx.care")
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "sender@scanx.care")
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("GCHAT_WEBHOOK_URL", "https://chat.example.com/hook")
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/Chicago")
    with psycopg.connect(DB, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS refund_queue, refund_excluded_appointment_type, appointment CASCADE")
        c.execute("""CREATE TABLE appointment (
            id serial PRIMARY KEY, appointment_id varchar(50) UNIQUE NOT NULL,
            appointment_type varchar(150), amount_paid numeric(10,2), paid boolean DEFAULT false,
            status_label varchar(50), canceled boolean DEFAULT false,
            first_name varchar(100), last_name varchar(100),
            phone varchar(20), email varchar(150), location varchar(100),
            appointment_datetime timestamptz, date timestamp, time varchar(20))""")
        for m in MIGRATIONS:
            c.execute(m.read_text())
        yield c


def add_cancelled(c, appt, amount="100.00", status="Cancelled", due_now=True, appt_type="Consultation", email="t@example.com"):
    c.execute("INSERT INTO appointment (appointment_id, appointment_type, amount_paid, paid, status_label, "
              "first_name, last_name, phone, email, location, appointment_datetime) "
              "VALUES (%s, %s, %s, true, 'Scheduled', 'Test', 'Tester', '+916355325189', %s, "
              "'Dallas Location', '2026-11-17 14:45:00+00')", (appt, appt_type, amount, email))
    c.execute("UPDATE appointment SET status_label = %s WHERE appointment_id = %s", (status, appt))
    if due_now:
        c.execute("UPDATE refund_queue SET due_at = now() - interval '1 second' WHERE appointment_id = %s", (appt,))


def queue(c, appt):
    return c.execute("SELECT status, stripe_payment_intent, stripe_refund_id, last_error, attempts "
                     "FROM refund_queue WHERE appointment_id = %s ORDER BY id DESC LIMIT 1", (appt,)).fetchone()


@pytest.fixture
def fakes(monkeypatch):
    calls = {"refunds": [], "payments": {}, "emails": [], "cards": [], "email_down": False, "gchat_down": False}

    def payments(appt):
        result = calls["payments"].get(appt, [])
        if isinstance(result, Exception):
            raise result
        return result

    def refund(api_key, idempotency_key, **params):
        calls["refunds"].append((idempotency_key, params))
        return SimpleNamespace(id=f"re_{len(calls['refunds'])}", status="succeeded")

    def send_email(msg):
        if calls["email_down"]:
            raise OSError("smtp down")
        calls["emails"].append(msg)

    def post_gchat(card):
        if calls["gchat_down"]:
            raise RuntimeError("google chat 503")
        calls["cards"].append(card)

    monkeypatch.setattr(notify, "_send_email", send_email)
    monkeypatch.setattr(notify, "_post_gchat", post_gchat)
    monkeypatch.setattr(worker, "acuity_stripe_payments", payments)
    monkeypatch.setattr(stripe.Refund, "create", refund)
    return calls


def test_refunds_due_cancellation(conn, fakes):
    add_cancelled(conn, "A1")
    fakes["payments"]["A1"] = [worker.Payment("ch_1", Decimal("100.00"))]
    assert worker.run_once() == 1
    assert queue(conn, "A1")[:3] == ("succeeded", "ch_1", "re_1")
    key, params = fakes["refunds"][0]
    assert key == "automated_refund_processing-A1-ch_1" and params["charge"] == "ch_1"


def test_payment_intent_and_no_show(conn, fakes):
    add_cancelled(conn, "A2", status="No Show")
    fakes["payments"]["A2"] = [worker.Payment("pi_9", Decimal("100.00"))]
    worker.run_once()
    assert fakes["refunds"][0][1]["payment_intent"] == "pi_9"


def add_scheduled(c, appt, *, paid=True, label="Scheduled", canceled=False, appt_type="Consultation"):
    c.execute("INSERT INTO appointment (appointment_id, appointment_type, amount_paid, paid, status_label, canceled, "
              "first_name, last_name, location, appointment_datetime) "
              "VALUES (%s, %s, 100, %s, %s, %s, 'Test', 'Tester', 'Dallas Location', '2026-11-17 14:45:00+00')",
              (appt, appt_type, paid, label, canceled))


def test_canceled_flag_queues_and_refunds(conn, fakes):
    add_scheduled(conn, "C1")
    assert queue(conn, "C1") is None
    conn.execute("UPDATE appointment SET canceled = true WHERE appointment_id = 'C1'")
    assert queue(conn, "C1")[0] == "pending"
    assert conn.execute("SELECT trigger_status FROM refund_queue WHERE appointment_id = 'C1'").fetchone()[0] == "Canceled"
    conn.execute("UPDATE refund_queue SET due_at = now() - interval '1 second'")
    fakes["payments"]["C1"] = [worker.Payment("ch_c1", Decimal("100.00"))]
    assert worker.run_once() == 1
    assert queue(conn, "C1")[0] == "succeeded"


def test_canceled_flag_with_empty_label_does_not_break_the_update(conn, fakes):
    add_scheduled(conn, "C2", label=None)
    conn.execute("UPDATE appointment SET canceled = true WHERE appointment_id = 'C2'")
    assert queue(conn, "C2")[0] == "pending"


def test_canceled_flag_already_set_on_insert(conn, fakes):
    add_scheduled(conn, "C3", canceled=True)
    assert queue(conn, "C3")[0] == "pending"


def test_canceled_flag_reverted_cancels_the_refund(conn, fakes):
    add_scheduled(conn, "C4", canceled=True)
    conn.execute("UPDATE appointment SET canceled = false WHERE appointment_id = 'C4'")
    assert queue(conn, "C4")[0] == "cancelled"
    conn.execute("UPDATE refund_queue SET due_at = now() - interval '1 second'")
    assert worker.run_once() == 0 and fakes["refunds"] == []


def test_canceled_flag_ignored_when_unpaid_or_excluded(conn, fakes):
    add_scheduled(conn, "C5", paid=False, canceled=True)
    add_scheduled(conn, "C6", canceled=True, appt_type="IVF Follow-Up Ultrasound")
    assert queue(conn, "C5") is None and queue(conn, "C6") is None


def test_label_still_works_when_flag_is_false_or_null(conn, fakes):
    add_scheduled(conn, "C7", canceled=None)
    conn.execute("UPDATE appointment SET status_label = 'No Show' WHERE appointment_id = 'C7'")
    assert queue(conn, "C7")[0] == "pending"


def test_flag_reverted_after_queued_but_label_still_cancelled_keeps_refund(conn, fakes):
    add_scheduled(conn, "C8", label="Cancelled", canceled=True)
    conn.execute("UPDATE appointment SET canceled = false WHERE appointment_id = 'C8'")
    assert queue(conn, "C8")[0] == "pending"  # the label still says cancelled


def test_not_due_yet_is_left_alone(conn, fakes):
    add_cancelled(conn, "A3", due_now=False)
    assert worker.run_once() == 0
    assert queue(conn, "A3")[0] == "pending"


def test_reverted_status_never_refunds(conn, fakes):
    add_cancelled(conn, "A4")
    conn.execute("UPDATE appointment SET status_label = 'Scheduled' WHERE appointment_id = 'A4'")
    assert worker.run_once() == 0
    assert queue(conn, "A4")[0] == "cancelled"
    assert fakes["refunds"] == []


def test_no_acuity_payment_goes_to_review(conn, fakes):
    add_cancelled(conn, "A5")
    worker.run_once()
    assert queue(conn, "A5")[0] == "needs_review"


def test_transient_error_retries_with_backoff(conn, fakes):
    add_cancelled(conn, "A6")
    fakes["payments"]["A6"] = worker.TransientError("acuity 503")
    worker.run_once()
    status, _, _, err, attempts = queue(conn, "A6")
    assert (status, attempts) == ("pending", 1) and "503" in err
    assert worker.run_once() == 0  # backed off, not due yet


def test_amount_mismatch_is_noted(conn, fakes):
    add_cancelled(conn, "A7", amount="100.00")
    fakes["payments"]["A7"] = [worker.Payment("ch_7", Decimal("80.00"))]
    worker.run_once()
    status, *_, err, _ = queue(conn, "A7")
    assert status == "succeeded" and "80.00" in err


def test_second_run_does_not_refund_again(conn, fakes):
    add_cancelled(conn, "A8")
    fakes["payments"]["A8"] = [worker.Payment("ch_8", Decimal("100.00"))]
    worker.run_once()
    worker.run_once()
    assert len(fakes["refunds"]) == 1


@pytest.mark.parametrize("appt_type", [
    "IVF Monitoring Package", "IVF Follow-Up Ultrasound", " ivf monitoring package ",
    "IVF Follow Up Ultrasound", "ivf followup ultrasound", "IVF-Monitoring  Package",
    "IVF Follow-Up Ultrasound.", "IVF MONITORING PACKAGE",
])
def test_ivf_types_are_never_queued(conn, fakes, appt_type):
    add_cancelled(conn, "A9", appt_type=appt_type)
    assert queue(conn, "A9") is None
    assert worker.run_once() == 0
    assert fakes["refunds"] == []


@pytest.mark.parametrize("appt_type", ["Consultation", "IVF Consultation", None, "", "---"])
def test_other_types_are_still_queued(conn, fakes, appt_type):
    add_cancelled(conn, "A9b", appt_type=appt_type)
    assert queue(conn, "A9b")[0] == "pending"


def test_type_changed_to_excluded_after_queueing(conn, fakes):
    add_cancelled(conn, "A10")
    conn.execute("UPDATE appointment SET appointment_type = 'IVF Follow-Up Ultrasound' WHERE appointment_id = 'A10'")
    fakes["payments"]["A10"] = [worker.Payment("ch_10", Decimal("100.00"))]
    worker.run_once()
    assert queue(conn, "A10")[0] == "cancelled"
    assert fakes["refunds"] == []


def card_text(card):
    return " ".join(w["textParagraph"]["text"] for sec in card["cardsV2"][0]["card"]["sections"]
                    for w in sec["widgets"] if "textParagraph" in w)


def test_success_alert_has_everything(conn, fakes):
    add_cancelled(conn, "N1", amount="699.00", status="No Show")
    fakes["payments"]["N1"] = [worker.Payment("ch_n1", Decimal("699.00"))]
    worker.run_once()

    assert len(fakes["emails"]) == 1 and len(fakes["cards"]) == 1
    card = fakes["cards"][0]["cardsV2"][0]["card"]
    assert card["header"]["title"].startswith("Refund Succeeded")
    text = card_text(fakes["cards"][0])
    for expected in ["<b>Patient Name:</b> Test Tester", "<b>Appointment Date:</b> November 17, 2026",
                     "<b>Day:</b> Tuesday", "<b>Time:</b> 8:45 AM CST", "<b>Reason:</b> No-show",
                     "<b>Amount Refunded:</b> $699.00", "<b>Phone:</b> +916355325189",
                     "<b>Email:</b> t@example.com", "<b>Queue ID:</b>", "<b>Stripe Refund:</b> re_1",
                     "<b>Trigger Status:</b> No Show"]:
        assert expected in text, expected
    buttons = fakes["cards"][0]["cardsV2"][0]["card"]["sections"][-1]["widgets"][0]["buttonList"]["buttons"]
    assert buttons[0]["onClick"]["openLink"]["url"] == "tel:+916355325189"
    assert buttons[1]["onClick"]["openLink"]["url"] == "https://dashboard.stripe.com/test/refunds/re_1"

    msg = fakes["emails"][0]
    assert msg["To"] == "devuser1@scanx.care, clinic1@scanx.care"
    assert msg["From"] == "ScanX Health LLC <sender@scanx.care>"
    assert msg["Subject"] == "✅ Refund Succeeded: Test Tester · $699.00 · November 17, 2026"
    assert "Tuesday" in msg.get_body(("html",)).get_content()

    worker.run_once()  # nothing sent twice
    assert len(fakes["emails"]) == 1 and len(fakes["cards"]) == 1


def test_needs_review_alert_and_missing_email(conn, fakes):
    add_cancelled(conn, "N2", email=None)
    worker.run_once()  # no Acuity payment -> needs_review
    text = card_text(fakes["cards"][0])
    assert fakes["cards"][0]["cardsV2"][0]["card"]["header"]["title"].startswith("Refund Needs Review")
    assert "<b>Amount Refunded:</b> $0.00 (not refunded)" in text
    assert "<b>Problem:</b> no Stripe payment recorded in Acuity" in text
    assert "<b>Email:</b>" not in text
    assert fakes["emails"][0]["Subject"].startswith("⚠️ Refund Needs Review: Test Tester")


def test_failed_refund_alerts(conn, fakes, monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "1")
    add_cancelled(conn, "N3")
    fakes["payments"]["N3"] = worker.TransientError("acuity 503")
    worker.run_once()
    assert queue(conn, "N3")[0] == "failed"
    assert fakes["cards"][0]["cardsV2"][0]["card"]["header"]["title"].startswith("Refund Failed")


def test_no_alert_for_cancelled_or_pending(conn, fakes):
    add_cancelled(conn, "N4")
    conn.execute("UPDATE appointment SET status_label = 'Scheduled' WHERE appointment_id = 'N4'")
    add_cancelled(conn, "N5", due_now=False)
    worker.run_once()
    assert fakes["emails"] == [] and fakes["cards"] == []


def test_channel_down_retries_only_that_channel(conn, fakes):
    add_cancelled(conn, "N6")
    fakes["payments"]["N6"] = [worker.Payment("ch_n6", Decimal("100.00"))]
    fakes["gchat_down"] = True
    worker.run_once()
    assert len(fakes["emails"]) == 1 and fakes["cards"] == []
    row = conn.execute("SELECT notify_attempts, notify_error FROM refund_queue WHERE appointment_id = 'N6'").fetchone()
    assert row[0] == 1 and "gchat" in row[1]

    fakes["gchat_down"] = False
    worker.run_once()
    assert len(fakes["emails"]) == 1 and len(fakes["cards"]) == 1
    row = conn.execute("SELECT notify_error, gchat_notified_at IS NOT NULL FROM refund_queue WHERE appointment_id = 'N6'").fetchone()
    assert row == (None, True)


def test_alert_failure_never_blocks_refunds(conn, fakes):
    fakes["email_down"] = fakes["gchat_down"] = True
    add_cancelled(conn, "N7")
    fakes["payments"]["N7"] = [worker.Payment("ch_n7", Decimal("100.00"))]
    worker.run_once()
    assert queue(conn, "N7")[0] == "succeeded"


def test_gmail_api_send(monkeypatch):
    """_send_email posts the base64url MIME message to Gmail with the delegated token."""
    import base64
    from email import message_from_bytes
    from email.message import EmailMessage

    monkeypatch.setattr(notify, "_gmail_token", lambda: "tok")
    posted = {}

    def post(url, headers, json, timeout):
        posted.update(url=url, headers=headers, json=json)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(notify.requests, "post", post)
    msg = EmailMessage()
    msg["Subject"] = "hi"
    msg.set_content("body")
    notify._send_email(msg)
    assert posted["url"] == notify.GMAIL_SEND_URL
    assert posted["headers"] == {"Authorization": "Bearer tok"}
    assert message_from_bytes(base64.urlsafe_b64decode(posted["json"]["raw"]))["Subject"] == "hi"

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=403, text="", json=lambda: {"error": {"message": "Delegation denied"}}))
    with pytest.raises(RuntimeError, match="Delegation denied"):
        notify._send_email(msg)


# --- Patient SMS ------------------------------------------------------------

@pytest.fixture
def texts(monkeypatch, fakes):
    from automated_refund_processing import sms
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12145550100")
    monkeypatch.setenv("CLINIC_PHONE", "(214) 555-0199")
    sent = {"messages": [], "error": None}

    def send(to, body):
        if sent["error"]:
            raise sent["error"]
        sent["messages"].append((to, body))

    monkeypatch.setattr(sms, "_send_sms", send)
    return sent


def refunded(conn, fakes, appt, **kw):
    add_cancelled(conn, appt, **kw)
    fakes["payments"][appt] = [worker.Payment(f"ch_{appt}", Decimal("100.00"))]
    worker.run_once()


def test_patient_gets_the_refund_text(conn, fakes, texts):
    refunded(conn, fakes, "S1")
    assert texts["messages"] == [(
        "+916355325189",
        "ScanX Health: Your refund of $100.00 for your November 17, 2026 at 8:45 AM CST appointment, "
        "which was cancelled, has been issued. For questions, please call (214) 555-0199. "
        "Receipt of this transaction is sent to you via your registered email account.")]
    assert conn.execute("SELECT sms_notified_at IS NOT NULL, sms_attempts FROM refund_queue "
                        "WHERE appointment_id = 'S1'").fetchone() == (True, 0)
    worker.run_once()
    assert len(texts["messages"]) == 1  # never texted twice


def test_no_show_text_says_so(conn, fakes, texts):
    refunded(conn, fakes, "S1b", status="No Show")
    assert "appointment, which was a no-show, has been issued." in texts["messages"][0][1]


@pytest.mark.parametrize("email", [None, "", "   "])
def test_text_without_patient_email_drops_the_receipt_line(conn, fakes, texts, email):
    refunded(conn, fakes, "S1c", email=email)
    assert texts["messages"] == [(
        "+916355325189",
        "ScanX Health: Your refund of $100.00 for your November 17, 2026 at 8:45 AM CST appointment, "
        "which was cancelled, has been issued. For questions, please call (214) 555-0199.")]


def test_no_text_unless_refund_succeeded(conn, fakes, texts):
    add_cancelled(conn, "S2")  # no Stripe payment -> needs_review
    add_cancelled(conn, "S3", due_now=False)  # still pending
    worker.run_once()
    assert queue(conn, "S2")[0] == "needs_review" and texts["messages"] == []


def test_already_refunded_by_hand_is_not_texted(conn, fakes, texts, monkeypatch):
    monkeypatch.setattr(stripe.Refund, "create", lambda **k: (_ for _ in ()).throw(
        stripe.InvalidRequestError("already", "charge", code="charge_already_refunded")))
    refunded(conn, fakes, "S4")
    assert queue(conn, "S4")[0] == "succeeded" and texts["messages"] == []


def test_twilio_down_retries_then_sends(conn, fakes, texts):
    texts["error"] = RuntimeError("twilio 503")
    refunded(conn, fakes, "S5")
    assert queue(conn, "S5")[0] == "succeeded" and texts["messages"] == []
    assert conn.execute("SELECT sms_attempts, sms_error FROM refund_queue WHERE appointment_id = 'S5'"
                        ).fetchone() == (1, "twilio 503")
    texts["error"] = None
    worker.run_once()
    assert len(texts["messages"]) == 1
    assert conn.execute("SELECT sms_error FROM refund_queue WHERE appointment_id = 'S5'").fetchone() == (None,)


def test_bad_phone_is_recorded_and_not_retried(conn, fakes, texts):
    refunded(conn, fakes, "S6")
    conn.execute("UPDATE refund_queue SET sms_notified_at = NULL, sms_attempts = 0 WHERE appointment_id = 'S6'")
    conn.execute("UPDATE appointment SET phone = 'n/a' WHERE appointment_id = 'S6'")
    texts["messages"].clear()
    worker.run_once()
    row = conn.execute("SELECT sms_attempts, sms_error FROM refund_queue WHERE appointment_id = 'S6'").fetchone()
    assert row[0] == notify.MAX_NOTIFY_ATTEMPTS and "phone" in row[1] and texts["messages"] == []


def test_sms_failure_never_blocks_refund_or_alerts(conn, fakes, texts):
    texts["error"] = RuntimeError("twilio down")
    refunded(conn, fakes, "S7")
    assert queue(conn, "S7")[0] == "succeeded"
    assert len(fakes["emails"]) == 1 and len(fakes["cards"]) == 1


def test_sms_off_without_twilio_settings(conn, fakes):
    refunded(conn, fakes, "S8")
    assert queue(conn, "S8")[0] == "succeeded"
    assert conn.execute("SELECT sms_notified_at, sms_attempts FROM refund_queue "
                        "WHERE appointment_id = 'S8'").fetchone() == (None, 0)


@pytest.mark.parametrize("raw,expected", [
    ("(214) 555-0100", "+12145550100"), ("214-555-0100", "+12145550100"),
    ("1 214 555 0100", "+12145550100"), ("+12145550100", "+12145550100"),
    ("+91 63553 25189", "+916355325189"),
])
def test_phone_to_e164(raw, expected):
    from automated_refund_processing import sms
    assert sms.to_e164(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "n/a", "555-0100", "12345"])
def test_phone_to_e164_rejects_junk(raw):
    from automated_refund_processing import sms
    with pytest.raises(sms.PermanentError):
        sms.to_e164(raw)


def test_twilio_request_and_errors(monkeypatch):
    from automated_refund_processing import sms
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12145550100")
    seen = {}

    def post(url, data, auth, timeout):
        seen.update(url=url, data=data, auth=auth)
        return SimpleNamespace(status_code=seen.get("status", 201), text="",
                               json=lambda: {"code": 21211, "message": "Invalid 'To'"})

    monkeypatch.setattr(sms.requests, "post", post)
    sms._send_sms("+12145550111", "hi")
    assert seen["url"].endswith("/Accounts/ACx/Messages.json") and seen["auth"] == ("ACx", "tok")
    assert seen["data"] == {"To": "+12145550111", "Body": "hi", "From": "+12145550100"}
    seen["status"] = 400
    with pytest.raises(sms.PermanentError, match="21211"):
        sms._send_sms("+12145550111", "hi")
    for status in (429, 503):
        seen["status"] = status
        with pytest.raises(RuntimeError) as e:
            sms._send_sms("+12145550111", "hi")
        assert not isinstance(e.value, sms.PermanentError)
