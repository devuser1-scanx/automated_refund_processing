-- Least-privilege login for the Cloud Run Job. Run once as the owner of
-- `appointment`, with the password passed in:
--   psql ... -v worker_password="'<password>'" -f migrations/002_worker_db_role.sql
-- Then DATABASE_URL uses user automated_refund_processing.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automated_refund_processing') THEN
    CREATE ROLE automated_refund_processing LOGIN;
  END IF;
END
$$;

ALTER ROLE automated_refund_processing PASSWORD :worker_password;

GRANT SELECT ON appointment TO automated_refund_processing;
GRANT SELECT, UPDATE ON refund_queue TO automated_refund_processing;
GRANT SELECT ON refund_excluded_appointment_type TO automated_refund_processing;
