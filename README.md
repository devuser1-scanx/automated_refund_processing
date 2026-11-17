# automated_refund_processing

Full Stripe refund when a paid appointment is set to Cancelled / Canceled / No-Show,
10 to 15 minutes after the change, so mistakes can be undone first.
Appointment types in `refund_excluded_appointment_type` (IVF Monitoring Package,
IVF Follow-Up Ultrasound) are never auto-refunded.

Full design (diagrams, tables, edge cases): [docs/design.pdf](docs/design.pdf), live version https://claude.ai/artifact/Q3KdiPvL7w94vLM7nkZind

## Layout
- `migrations/001_automated_refund_processing.sql`: `refund_queue`, `refund_excluded_appointment_type`, functions, trigger `trg_queue_refund` (SECURITY DEFINER).
- `migrations/002_worker_db_role.sql`: least-privilege Postgres login `automated_refund_processing` for the job.
- `DEPLOY.md`: deploy runbook with every command.
- `automated_refund_processing/worker.py`: run every 5 minutes. Claims due rows, re-checks the appointment,
  gets the Stripe transaction id from Acuity (`GET /appointments/{id}/payments`), refunds it in full.
- `automated_refund_processing/notify.py`: email + Google Chat alert for every succeeded, failed and
  needs_review refund (patient, appointment date/day/time, reason, amount, phone, email, full queue record).
  Each channel is tracked separately and retried next run if it was down.
- `tests/test_worker.py`: runs against a scratch Postgres (`TEST_DATABASE_URL`), Acuity and Stripe faked.

## Run tests
```
pip install -r requirements.txt pytest
TEST_DATABASE_URL=postgresql://user@localhost/scratch python -m pytest tests
```

## Queue statuses
| status | meaning |
|---|---|
| pending | waiting for grace period (or a retry backoff) |
| processing | a worker run has it |
| succeeded | refunded; `stripe_refund_id` set (`already_refunded` if done by hand earlier) |
| cancelled | status reverted, or type excluded, before the refund ran |
| needs_review | no Stripe payment in Acuity, or Stripe rejected it. See `last_error` |
| failed | temporary errors 5 times in a row |

## Deploy (Google Cloud)
Step-by-step commands for project vernal-maker-473121-k4 are in [DEPLOY.md](DEPLOY.md).

## Alerts
| Setting | Example |
|---|---|
| `NOTIFY_EMAILS` | `devuser1@scanx.care,clinic1@scanx.care` |
| `GMAIL_SENDER_EMAIL`, `GMAIL_SERVICE_ACCOUNT_JSON` | existing Secret Manager secrets; Gmail API + domain-wide delegation (`gmail.send`), same as the other ScanX services |
| `GCHAT_WEBHOOK_URL` | incoming webhook of the Google Chat space (secret) |
| `DISPLAY_TIMEZONE` | `America/Chicago` |

A channel with no settings is skipped. Undelivered alerts are retried every run, up to 5 times (`notify_attempts`, `notify_error`).

## Handy queries
```sql
SELECT * FROM refund_queue WHERE status IN ('needs_review','failed') ORDER BY created_at DESC;
-- alerts that never got through
SELECT id, appointment_id, notify_error FROM refund_queue WHERE notify_attempts >= 5;
```
