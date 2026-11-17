## Deploy runbook

Run these from the repo root in Cloud Shell, which already has `gcloud`, `psql` and `cloud-sql-proxy`. Fill in the four placeholders in step 0. Everything else is ready to paste.

### 0. Variables

```bash
export PROJECT=vernal-maker-473121-k4
export REGION=<cloud-sql-region>          # same region as your Cloud SQL instance, e.g. us-central1
export INSTANCE=<cloud-sql-instance-name>
export DB_NAME=<database-name>
export DB_OWNER=<db-user-that-owns-appointment>
export SA=automated-refund-processing@$PROJECT.iam.gserviceaccount.com
gcloud config set project $PROJECT

gcloud sql instances list          # shows INSTANCE and REGION if unsure
```

### 1. Enable the APIs

```bash
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com sqladmin.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com
```

### 2. Database: tables, trigger and the worker's login

Run as the user that owns `appointment`, because only the owner can add a trigger to it.

```bash
cloud-sql-proxy $PROJECT:$REGION:$INSTANCE --port 5433 &
export DB="host=127.0.0.1 port=5433 dbname=$DB_NAME user=$DB_OWNER"

psql "$DB" -f migrations/001_automated_refund_processing.sql

export WORKER_DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
psql "$DB" -v worker_password="'$WORKER_DB_PASSWORD'" -f migrations/002_worker_db_role.sql

# checks
psql "$DB" -c "\d refund_queue"
psql "$DB" -c "SELECT DISTINCT appointment_type FROM appointment ORDER BY 1"   # IVF names must match exactly
```

The trigger runs as `SECURITY DEFINER`, so your app's DB user needs no new grants. The worker logs in as `automated_refund_processing`, which can only read `appointment` and read and update `refund_queue`.

### 3. Secrets

`GMAIL_SENDER_EMAIL` and `GMAIL_SERVICE_ACCOUNT_JSON` already exist. Create the other five, starting with the Stripe **test** key:

```bash
printf '%s' "postgresql://automated_refund_processing:$WORKER_DB_PASSWORD@/$DB_NAME?host=/cloudsql/$PROJECT:$REGION:$INSTANCE" \
  | gcloud secrets create automated-refund-processing-db-url --data-file=-
printf '%s' 'sk_test_...'                 | gcloud secrets create automated-refund-processing-stripe-key   --data-file=-
printf '%s' '<acuity-user-id>'            | gcloud secrets create automated-refund-processing-acuity-user  --data-file=-
printf '%s' '<acuity-api-key>'            | gcloud secrets create automated-refund-processing-acuity-key   --data-file=-
printf '%s' '<google-chat-webhook-url>'   | gcloud secrets create automated-refund-processing-gchat-webhook --data-file=-
```

### 4. Service account and permissions

```bash
gcloud iam service-accounts create automated-refund-processing \
  --display-name="automated_refund_processing job"

for s in automated-refund-processing-db-url automated-refund-processing-stripe-key \
         automated-refund-processing-acuity-user automated-refund-processing-acuity-key \
         automated-refund-processing-gchat-webhook GMAIL_SENDER_EMAIL GMAIL_SERVICE_ACCOUNT_JSON; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role=roles/cloudsql.client
```

### 5. Build and deploy the Cloud Run Job

`--source .` builds the `Dockerfile` with Cloud Build and stores the image in Artifact Registry. Run the same command again to deploy any later code change.

```bash
gcloud run jobs deploy automated-refund-processing \
  --source . --region $REGION \
  --service-account $SA \
  --set-cloudsql-instances $PROJECT:$REGION:$INSTANCE \
  --set-secrets DATABASE_URL=automated-refund-processing-db-url:latest,STRIPE_SECRET_KEY=automated-refund-processing-stripe-key:latest,ACUITY_USER_ID=automated-refund-processing-acuity-user:latest,ACUITY_API_KEY=automated-refund-processing-acuity-key:latest,GCHAT_WEBHOOK_URL=automated-refund-processing-gchat-webhook:latest,GMAIL_SENDER_EMAIL=GMAIL_SENDER_EMAIL:latest,GMAIL_SERVICE_ACCOUNT_JSON=GMAIL_SERVICE_ACCOUNT_JSON:latest \
  --set-env-vars "^;^NOTIFY_EMAILS=devuser1@scanx.care,clinic1@scanx.care;DISPLAY_TIMEZONE=America/Chicago;BATCH_SIZE=20;MAX_ATTEMPTS=5" \
  --tasks 1 --max-retries 0 --task-timeout 300s
```

`^;^` tells gcloud to split the env vars on `;`, because `NOTIFY_EMAILS` itself contains a comma.

### 6. Test it by hand before scheduling

```bash
# after staff mark a paid test appointment Cancelled, skip the 10 minute wait:
psql "$DB" -c "UPDATE refund_queue SET due_at = now() WHERE status = 'pending'"

gcloud run jobs execute automated-refund-processing --region $REGION --wait

gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="automated-refund-processing"' \
  --limit 50 --format='value(textPayload)'

psql "$DB" -c "SELECT id, appointment_id, status, stripe_refund_id, last_error, email_notified_at, gchat_notified_at FROM refund_queue ORDER BY id DESC LIMIT 5"
```

Acuity's payments are live charges, so a **test** Stripe key can't find them. In test mode, expect `needs_review` with "No such charge". That still proves the trigger, queue, Acuity lookup, email and Chat card all work. To prove a real refund, switch to the live key (step 8) and cancel a small appointment you paid for with your own card.

### 7. Schedule it every 5 minutes

```bash
gcloud run jobs add-iam-policy-binding automated-refund-processing --region $REGION \
  --member="serviceAccount:$SA" --role=roles/run.invoker

gcloud scheduler jobs create http automated-refund-processing-every-5-min \
  --location $REGION --schedule "*/5 * * * *" --time-zone "America/Chicago" \
  --uri "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/automated-refund-processing:run" \
  --http-method POST --oauth-service-account-email $SA
```

### 8. Go live

```bash
printf '%s' 'sk_live_...' | gcloud secrets versions add automated-refund-processing-stripe-key --data-file=-
```

Each run reads `:latest` when it starts, so the next run uses the live key without a redeploy.

### Day to day

| Task | Command |
| --- | --- |
| Pause all refunds | `gcloud scheduler jobs pause automated-refund-processing-every-5-min --location $REGION` |
| Resume | `gcloud scheduler jobs resume automated-refund-processing-every-5-min --location $REGION` |
| Run now | `gcloud run jobs execute automated-refund-processing --region $REGION --wait` |
| Deploy a code change | Re-run step 5 |
| Add an excluded type | `INSERT INTO refund_excluded_appointment_type (name) VALUES ('...');` |
| Change the grace period | `CREATE OR REPLACE FUNCTION refund_grace_period() RETURNS interval LANGUAGE sql IMMUTABLE AS $$ SELECT interval '15 minutes' $$;` |
| Retry a failed row | `UPDATE refund_queue SET status='pending', due_at=now(), attempts=0 WHERE id=<id>;` |
| Resend an alert | `UPDATE refund_queue SET email_notified_at=NULL, gchat_notified_at=NULL, notify_attempts=0 WHERE id=<id>;` |
| Rows needing a person | `SELECT * FROM refund_queue WHERE status IN ('needs_review','failed') ORDER BY id DESC;` |
