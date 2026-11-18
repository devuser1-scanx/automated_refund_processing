-- automated_refund_processing: database objects
--
-- When a paid appointment's status_label becomes cancelled / canceled / no-show,
-- or its boolean `canceled` column becomes true,
-- and its appointment_type is not excluded, a refund is queued with a grace period.
-- If staff revert the status before the grace period ends, the queued refund is
-- cancelled. The worker (automated_refund_processing/worker.py) picks up due rows
-- and refunds them in Stripe.
--
-- Safe to run more than once.

BEGIN;


-- Grace period lives in one place so it can be changed without touching the trigger.
CREATE OR REPLACE FUNCTION refund_grace_period() RETURNS interval
LANGUAGE sql IMMUTABLE AS $$ SELECT interval '10 minutes' $$;

-- Normalises labels like 'Cancelled', 'canceled', 'No Show', 'no-show', 'NoShow'.
CREATE OR REPLACE FUNCTION is_refundable_status(label text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(lower(coalesce(label, '')), '[^a-z]', '', 'g')
         IN ('cancelled', 'canceled', 'noshow')
$$;

-- An appointment counts as cancelled when its status_label is cancelled /
-- canceled / no-show OR its boolean `canceled` column is true.
CREATE OR REPLACE FUNCTION is_refundable_appointment(label text, canceled boolean) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(canceled, false) OR is_refundable_status(label)
$$;

CREATE TABLE IF NOT EXISTS refund_queue (
  id                     bigserial PRIMARY KEY,
  appointment_id         varchar(50) NOT NULL
                           REFERENCES appointment (appointment_id) ON DELETE RESTRICT,
  trigger_status         varchar(50) NOT NULL,      -- status_label that queued it
  amount                 numeric(10,2),             -- appointment.amount_paid at queue time
  status                 varchar(20) NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'processing', 'succeeded',
                                             'failed', 'cancelled', 'needs_review')),
  due_at                 timestamptz NOT NULL,
  attempts               integer NOT NULL DEFAULT 0,
  locked_at              timestamptz,
  stripe_payment_intent  varchar(255),
  stripe_refund_id       varchar(255),
  last_error             text,
  amount_refunded        numeric(10,2),             -- total Stripe refunded (succeeded rows)
  -- Alerts for succeeded / failed / needs_review rows. NULL = not sent yet.
  email_notified_at      timestamptz,
  gchat_notified_at      timestamptz,
  notify_attempts        integer NOT NULL DEFAULT 0,
  notify_error           text,
  -- Text message to the patient after a succeeded refund (automated_refund_processing/sms.py).
  sms_notified_at        timestamptz,
  sms_attempts           integer NOT NULL DEFAULT 0,
  sms_error              text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Upgrade path for a refund_queue created before the SMS columns existed.
-- Refunds that already succeeded are marked as texted, so deploying the SMS
-- feature never sends texts for old refunds.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = current_schema()
                    AND table_name = 'refund_queue' AND column_name = 'sms_notified_at') THEN
    ALTER TABLE refund_queue
      ADD COLUMN sms_notified_at timestamptz,
      ADD COLUMN sms_attempts    integer NOT NULL DEFAULT 0,
      ADD COLUMN sms_error       text;
    UPDATE refund_queue SET sms_notified_at = now() WHERE status = 'succeeded';
  END IF;
END $$;

-- At most one live or completed refund per appointment. Cancelled / failed /
-- needs_review rows don't block a new one, so cancel -> undo -> cancel works.
CREATE UNIQUE INDEX IF NOT EXISTS refund_queue_one_active_per_appt
  ON refund_queue (appointment_id)
  WHERE status IN ('pending', 'processing', 'succeeded');

-- Worker scan: due pending rows.
CREATE INDEX IF NOT EXISTS refund_queue_due
  ON refund_queue (due_at) WHERE status = 'pending';

-- Notifier scan: finished rows with an alert still to send.
CREATE INDEX IF NOT EXISTS refund_queue_unnotified
  ON refund_queue (id)
  WHERE status IN ('succeeded', 'failed', 'needs_review')
    AND (email_notified_at IS NULL OR gchat_notified_at IS NULL);


-- Appointment types that are never auto-refunded. Matched against
-- appointment.appointment_type ignoring case, spaces, hyphens and punctuation
-- ('IVF Follow-Up Ultrasound' = 'ivf followup  ultrasound'). To add one:
--   INSERT INTO refund_excluded_appointment_type (name) VALUES ('...');
CREATE TABLE IF NOT EXISTS refund_excluded_appointment_type (
  name        varchar(150) PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO refund_excluded_appointment_type (name) VALUES
  ('IVF Monitoring Package'),
  ('IVF Follow-Up Ultrasound')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION is_refund_excluded_type(appt_type text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT regexp_replace(lower(coalesce(appt_type, '')), '[^a-z0-9]', '', 'g') <> ''
     AND EXISTS (
    SELECT 1 FROM refund_excluded_appointment_type
     WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g')
         = regexp_replace(lower(coalesce(appt_type, '')), '[^a-z0-9]', '', 'g')
  )
$$;

-- SECURITY DEFINER: the trigger fires as whatever DB user your app saves
-- appointments with. Running as the migration's owner means that user needs
-- no grants on refund_queue.
CREATE OR REPLACE FUNCTION queue_refund_on_status_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  was_refundable boolean := TG_OP = 'UPDATE'
                            AND is_refundable_appointment(OLD.status_label, OLD.canceled)
                            AND coalesce(OLD.paid, false);
  now_refundable boolean := is_refundable_appointment(NEW.status_label, NEW.canceled)
                            AND coalesce(NEW.paid, false);
BEGIN
  IF now_refundable AND NOT was_refundable
     AND NOT is_refund_excluded_type(NEW.appointment_type) THEN
    INSERT INTO refund_queue (appointment_id, trigger_status, amount, due_at)
    VALUES (NEW.appointment_id,
            -- the label if it is cancelled / no-show, else 'Canceled' (the flag
            -- was set). Never NULL: trigger_status is NOT NULL.
            CASE WHEN is_refundable_status(NEW.status_label)
                 THEN NEW.status_label ELSE 'Canceled' END,
            NEW.amount_paid, now() + refund_grace_period())
    ON CONFLICT (appointment_id)
      WHERE status IN ('pending', 'processing', 'succeeded')
      DO NOTHING;

  ELSIF was_refundable AND NOT now_refundable THEN
    -- Mistake corrected within the grace period. Rows already being
    -- processed are left alone; the worker re-checks before refunding.
    UPDATE refund_queue
       SET status = 'cancelled', updated_at = now(),
           last_error = 'cancellation reverted (status_label=' || coalesce(NEW.status_label, 'NULL')
                        || ', canceled=' || coalesce(NEW.canceled::text, 'NULL') || ')'
     WHERE appointment_id = NEW.appointment_id
       AND status = 'pending';
  END IF;

  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_queue_refund ON appointment;
CREATE TRIGGER trg_queue_refund
  AFTER INSERT OR UPDATE OF status_label, paid, canceled ON appointment
  FOR EACH ROW EXECUTE FUNCTION queue_refund_on_status_change();

COMMIT;


-- Worker claim query (used in step 3), shown here for review.
-- SKIP LOCKED means two worker runs never grab the same row, and the join
-- re-checks that the appointment is still cancelled/no-show and paid.
--
--   UPDATE refund_queue q
--      SET status = 'processing', attempts = attempts + 1,
--          locked_at = now(), updated_at = now()
--    WHERE q.id IN (
--          SELECT r.id
--            FROM refund_queue r
--            JOIN appointment a USING (appointment_id)
--           WHERE r.status = 'pending'
--             AND r.due_at <= now()
--             AND is_refundable_status(a.status_label)
--             AND a.paid
--           ORDER BY r.due_at
--           LIMIT 20
--             FOR UPDATE OF r SKIP LOCKED)
--   RETURNING q.*;
