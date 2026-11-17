## Deploy runbook

Run from the repo root in Cloud Shell (it has `gcloud`, `psql` and `cloud-sql-proxy`). Fill in the placeholders in step 0.

This is a Cloud Run **Job**, not a Service. In the console it is under Cloud Run > **Jobs** tab, not the Services list. From the CLI: `gcloud run jobs list --region $REGION`.

Decisions baked into this runbook:
- The job reuses the existing shared secrets (table in step 3). Nothing new is created in Secret Manager.
- Stripe is the live key. There is no test phase, so nothing is executed by hand until step 6 says it is safe.
- Migration `002_worker_db_role.sql` is not used. The job logs in with the user inside `db-host`.

### 0. Variables

```bash
export PROJECT=vernal-maker-473121-k4
export REGION=<cloud-sql-region>          # e.g. us-central1; `gcloud sql instances list` shows it
export INSTANCE=<cloud-sql-instance-name>
export DB_NAME=<database-name>
export DB_OWNER=<db-user-that-owns-appointment>
export SA=automated-refund-processing@$PROJECT.iam.gserviceaccount.com
gcloud config set project $PROJECT
```

### 1. Enable the APIs

```bash
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com sqladmin.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com
```

### 2. Database: tables and trigger

Run as the user that owns `appointment`, because only the owner can add a trigger to it. Safe to run more than once. Nothing here is done by the deploy; it must be run by hand.

```bash
cloud-sql-proxy $PROJECT:$REGION:$INSTANCE --port 5433 &
export DB="host=127.0.0.1 port=5433 dbname=$DB_NAME user=$DB_OWNER"

psql "$DB" -f migrations/001_automated_refund_processing.sql
```

Check it:

```bash
psql "$DB" -c "SELECT tgname FROM pg_trigger WHERE tgname = 'trg_queue_refund'"
psql "$DB" -c "SELECT * FROM refund_excluded_appointment_type"
psql "$DB" -c "SELECT appointment_type, is_refund_excluded_type(appointment_type) AS excluded FROM (SELECT DISTINCT appointment_type FROM appointment) t ORDER BY 1"
psql "$DB" -c "SELECT tablename, tableowner FROM pg_tables WHERE tablename IN ('appointment','refund_queue')"
```

Expect the trigger, both IVF types, and `excluded = t` on the IVF types only. IVF names are matched ignoring case, spaces, hyphens and punctuation.

The job runs as the user inside the `db-host` secret. That user needs `SELECT` on `appointment`, `SELECT, UPDATE` on `refund_queue`, and `SELECT` on `refund_excluded_appointment_type`. If it owns the tables (for example it ran step 2), nothing more is needed. Otherwise run the three `GRANT` lines from `migrations/002_worker_db_role.sql` for that user.

The trigger is `SECURITY DEFINER`, so the app's own DB user needs no new grants. There is no backfill: only status changes made after this migration are queued.

### 3. Secrets (all already exist)

| Env var | Secret |
| --- | --- |
| `DATABASE_URL` | `db-host` |
| `STRIPE_SECRET_KEY` | `scanx-stripe-secret-key` (live key) |
| `GCHAT_WEBHOOK_URL` | `GCHAT_WEBHOOK_Automated_refund_transactions` |
| `ACUITY_USER_ID` | `ACUITY_USER_ID` |
| `ACUITY_API_KEY` | `ACUITY_API_KEY` |
| `GMAIL_SENDER_EMAIL` | `GMAIL_SENDER_EMAIL` |
| `GMAIL_SERVICE_ACCOUNT_JSON` | `GMAIL_SERVICE_ACCOUNT_JSON` |

These are shared with other ScanX services. Never add a new version to them from this project. `db-host` must hold a full connection string (`postgresql://user:pass@/db?host=/cloudsql/PROJECT:REGION:INSTANCE`), and the instance in it must be the same one passed to `--set-cloudsql-instances` in step 5.

Confirm they exist:

```bash
gcloud secrets list --filter="name~db-host OR name~scanx-stripe OR name~GCHAT_WEBHOOK_Automated OR name~ACUITY OR name~GMAIL" --format="value(name)"
```

### 4. Service account and permissions

```bash
gcloud iam service-accounts create automated-refund-processing \
  --display-name="automated_refund_processing job"
# "already exists" is fine, carry on

for s in db-host scanx-stripe-secret-key ACUITY_USER_ID ACUITY_API_KEY \
         GCHAT_WEBHOOK_Automated_refund_transactions GMAIL_SENDER_EMAIL GMAIL_SERVICE_ACCOUNT_JSON; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role=roles/cloudsql.client
```

This only gives this one service account read access to the secrets. It does not change them. If a binding fails with "permission denied", your account lacks admin on that secret: ask its owner to run it.

Your own account (the one running these commands) also needs permission to deploy: `roles/run.admin`, and `roles/iam.serviceAccountUser` on `$SA`:

```bash
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="user:<your-google-account-email>" --role=roles/iam.serviceAccountUser
```

### 5. Build and deploy the Cloud Run Job

`--source .` builds the `Dockerfile` with Cloud Build and stores the image in Artifact Registry. This only creates the job; it does not run it. Run the same command again to deploy any later code change.

```bash
gcloud run jobs deploy automated-refund-processing \
  --source . --region $REGION \
  --service-account $SA \
  --set-cloudsql-instances $PROJECT:$REGION:$INSTANCE \
  --set-secrets DATABASE_URL=db-host:latest,STRIPE_SECRET_KEY=scanx-stripe-secret-key:latest,ACUITY_USER_ID=ACUITY_USER_ID:latest,ACUITY_API_KEY=ACUITY_API_KEY:latest,GCHAT_WEBHOOK_URL=GCHAT_WEBHOOK_Automated_refund_transactions:latest,GMAIL_SENDER_EMAIL=GMAIL_SENDER_EMAIL:latest,GMAIL_SERVICE_ACCOUNT_JSON=GMAIL_SERVICE_ACCOUNT_JSON:latest \
  --set-env-vars "^;^NOTIFY_EMAILS=devuser1@scanx.care,clinic1@scanx.care;DISPLAY_TIMEZONE=America/Chicago;BATCH_SIZE=20;MAX_ATTEMPTS=5" \
  --tasks 1 --max-retries 0 --task-timeout 300s
```

`^;^` tells gcloud to split the env vars on `;`, because `NOTIFY_EMAILS` itself contains a comma.

Common failures:
- `Permission denied on secret ...`: step 4 binding is missing for that secret. Wait a minute after fixing it, then redeploy.
- Cloud Build permission errors: the error names the missing role; grant it to the Cloud Build or default compute service account it names.

### 6. Smoke test (optional, only with an empty queue)

The Stripe key is live, so a manual run refunds anything that is due. Check first:

```bash
psql "$DB" -c "SELECT count(*) FROM refund_queue"
```

Only if it prints `0`:

```bash
gcloud run jobs execute automated-refund-processing --region $REGION --wait

gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="automated-refund-processing"' \
  --limit 20 --format='value(textPayload)'
```

Expect `processed 0 refund(s)`. This proves the secrets, DB connection and permissions. If the count is not `0`, skip this step.

### 7. Check the trigger on real data before scheduling

Do not create the scheduler yet. Wait for a real cancellation or no-show on a paid, non-IVF appointment, then:

```bash
psql "$DB" -c "SELECT id, appointment_id, status, due_at, last_error FROM refund_queue ORDER BY id DESC LIMIT 5"
```

A `pending` row with `due_at` about 10 minutes out means the trigger works. If staff revert the status, the row should become `cancelled`. A cancelled IVF type should create no row.

### 8. Schedule it every 5 minutes (this is what makes refunds live)

```bash
gcloud run jobs add-iam-policy-binding automated-refund-processing --region $REGION \
  --member="serviceAccount:$SA" --role=roles/run.invoker

gcloud scheduler jobs create http automated-refund-processing-every-5-min \
  --location $REGION --schedule "*/5 * * * *" --time-zone "America/Chicago" \
  --uri "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/automated-refund-processing:run" \
  --http-method POST --oauth-service-account-email $SA
```

The first alert email and Google Chat card for a real refund show the whole path works. Each run reads the secrets at `:latest` when it starts, so rotating a secret needs no redeploy.

### Day to day

| Task | Command |
| --- | --- |
| Pause all refunds | `gcloud scheduler jobs pause automated-refund-processing-every-5-min --location $REGION` |
| Resume | `gcloud scheduler jobs resume automated-refund-processing-every-5-min --location $REGION` |
| Run now (refunds anything due) | `gcloud run jobs execute automated-refund-processing --region $REGION --wait` |
| Deploy a code change | Re-run step 5 |
| Add an excluded type | `INSERT INTO refund_excluded_appointment_type (name) VALUES ('...');` |
| Change the grace period | `CREATE OR REPLACE FUNCTION refund_grace_period() RETURNS interval LANGUAGE sql IMMUTABLE AS $$ SELECT interval '15 minutes' $$;` |
| Retry a failed row | `UPDATE refund_queue SET status='pending', due_at=now(), attempts=0 WHERE id=<id>;` |
| Resend an alert | `UPDATE refund_queue SET email_notified_at=NULL, gchat_notified_at=NULL, notify_attempts=0 WHERE id=<id>;` |
| Rows needing a person | `SELECT * FROM refund_queue WHERE status IN ('needs_review','failed') ORDER BY id DESC;` |

`To execute the job:` gcloud run jobs execute automated-refund-processing