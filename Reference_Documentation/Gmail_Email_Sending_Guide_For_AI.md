# Sending email from a backend through the Gmail API (service account + domain-wide delegation)

Audience: an AI coding assistant (or developer) re-implementing the exact email-sending feature
used by the ScanX Command Center backend in another application.
Source of truth in the original project: `scanx_command_center_BE/app/integrations/gmail_client.py`.

## 1. What it is, in one paragraph

The backend sends real email (HTML body, optional attachments, to/cc/bcc) by calling the **Gmail REST
API** (`users.messages.send`) as a **Google Workspace mailbox**. It does NOT use SMTP and does NOT use
an interactive OAuth login. Authentication is a **Google Cloud service account** that uses
**domain-wide delegation** to impersonate one Workspace mailbox (the "sender"). The service account's
JSON key is stored as a secret and loaded into an environment variable. Every send: build a MIME message
-> base64url-encode it -> get a short-lived access token for the impersonated sender -> POST to Gmail.

## 2. Moving parts

| Piece | Value in the original project |
|---|---|
| Transport | HTTPS REST: `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send` (no SMTP) |
| Auth | Service account JWT -> OAuth2 access token, impersonating the sender (`with_subject`) |
| OAuth scope | `https://www.googleapis.com/auth/gmail.send` (send-only; cannot read mail) |
| Libraries | `google-auth` (service account creds + token refresh), `httpx` (the HTTP call), Python stdlib `email` (MIME building). `google-auth`'s requests transport also needs the `requests` package installed. |
| Sender mailbox | env `GMAIL_SENDER_EMAIL` (must be a real mailbox on the Workspace domain) |
| Display name | constant `GMAIL_SENDER_DISPLAY_NAME = "ScanX Health LLC"` in code (From: `ScanX Health LLC <sender>`) |
| Credentials | env `GMAIL_SERVICE_ACCOUNT_JSON` = the full service-account key JSON as a single-line string |
| Runtime | FastAPI on Google Cloud Run; secrets injected from Google Secret Manager |
| Concurrency | Synchronous, blocking call (`httpx.Client`, 60 s timeout). Called inline from request handlers. |

## 3. Where the credentials live

Two settings, read through the app's settings object (`app/core/config.py`, pydantic settings):

```python
gmail_sender_email: str | None = None          # env GMAIL_SENDER_EMAIL
gmail_service_account_json: str | None = None  # env GMAIL_SERVICE_ACCOUNT_JSON
```

- **Local development:** both are set in the backend's `.env` file (git-ignored). `.env.example` lists the
  names with empty values and comments. `GMAIL_SERVICE_ACCOUNT_JSON` is the whole key file contents
  collapsed to ONE line (e.g. `{"type":"service_account","project_id":"...","private_key":"-----BEGIN
  PRIVATE KEY-----\n...","client_email":"...","client_id":"..."}`; keep the `\n` escapes inside the
  private key).
- **Deployed (staging and prod):** two **Google Secret Manager** secrets with the same names,
  `GMAIL_SENDER_EMAIL` and `GMAIL_SERVICE_ACCOUNT_JSON`, mapped into the Cloud Run service in the CI/CD
  deploy step with:
  `--set-secrets=GMAIL_SENDER_EMAIL=GMAIL_SENDER_EMAIL:latest,GMAIL_SERVICE_ACCOUNT_JSON=GMAIL_SERVICE_ACCOUNT_JSON:latest`
  (The Cloud Run runtime service account needs `roles/secretmanager.secretAccessor` on them.)
- The key is never committed to git, never logged, and the code never writes it to disk: it is parsed
  from the env string with `json.loads` and used in memory.
- If either value is missing, the code raises `RuntimeError("GMAIL_SENDER_EMAIL and
  GMAIL_SERVICE_ACCOUNT_JSON must be configured to send email.")`.

## 4. One-time setup (Google side)

1. In Google Cloud, pick/create a project and **enable the Gmail API**.
2. Create a **service account** (no special IAM roles needed for Gmail itself).
3. Create a **JSON key** for it and download it. Collapse to a single line for the env var / secret.
4. Note the service account's **Client ID** (the numeric "Unique ID" in the key's `client_id`).
5. In **Google Workspace Admin Console** -> Security -> Access and data control -> API controls ->
   **Domain-wide delegation** -> Add new: Client ID = step 4, OAuth scope =
   `https://www.googleapis.com/auth/gmail.send`. (Requires a Workspace super admin.)
6. Choose the **sender mailbox** (a real user/shared mailbox on the Workspace domain, e.g.
   `no-reply@yourdomain.com`) and set `GMAIL_SENDER_EMAIL` to it.
7. Store the two values as env vars / secrets as described in section 3.

Requirements and limits of this approach:
- Needs **Google Workspace** (domain-wide delegation does not exist for consumer @gmail.com accounts).
- The impersonated sender must exist; Gmail sending limits of that mailbox apply (Workspace daily send
  limits; recipients/day).
- Anyone who can read the key JSON can send mail as that mailbox: treat it as a high-value secret.

## 5. The complete implementation (copy this)

```python
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.core.config import settings  # provides gmail_sender_email, gmail_service_account_json

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SENDER_DISPLAY_NAME = "ScanX Health LLC"


@dataclass(frozen=True)
class EmailAttachment:  # in the original project this class is called FaxAttachment
    filename: str
    content: bytes
    content_type: str


class GmailApiError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _get_access_token() -> str:
    """Short-lived access token for GMAIL_SENDER_EMAIL via domain-wide delegation."""
    if not (settings.gmail_sender_email and settings.gmail_service_account_json):
        raise RuntimeError(
            "GMAIL_SENDER_EMAIL and GMAIL_SERVICE_ACCOUNT_JSON must be configured to send email."
        )

    try:
        info = json.loads(settings.gmail_service_account_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GMAIL_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=[GMAIL_SEND_SCOPE],
    ).with_subject(settings.gmail_sender_email)  # <- impersonate the sender mailbox

    credentials.refresh(Request())  # network call to Google's token endpoint

    if not credentials.token:
        raise GmailApiError("Could not obtain a Gmail access token.")

    return credentials.token


def send_email(
    *,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    html_body: str,
    attachments: list[EmailAttachment],
) -> str:
    """Sends one email via the Gmail API from GMAIL_SENDER_EMAIL. Returns the Gmail message id."""
    if not to:
        raise ValueError("At least one recipient is required.")

    message = MIMEMultipart("mixed")
    message["From"] = formataddr((GMAIL_SENDER_DISPLAY_NAME, settings.gmail_sender_email))
    message["To"] = ", ".join(to)

    if cc:
        message["Cc"] = ", ".join(cc)

    if bcc:
        message["Bcc"] = ", ".join(bcc)  # Gmail delivers to Bcc and strips the header

    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html"))

    for attachment in attachments:
        part = MIMEApplication(attachment.content)
        part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    access_token = _get_access_token()

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )

    if response.status_code >= 400:
        try:
            error_message = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            error_message = response.text

        raise GmailApiError(error_message)

    message_id = response.json().get("id")

    if not message_id:
        raise GmailApiError("Gmail response did not include a message id.")

    return message_id
```

Dependencies to add: `google-auth`, `httpx`, `requests` (needed by `google.auth.transport.requests`).

## 6. How callers use it

```python
try:
    message_id = send_email(
        to=["person@example.com"],
        cc=["archive@yourdomain.com"],
        bcc=[],
        subject="Hello",
        html_body="<p>Hi there</p>",
        attachments=[EmailAttachment("report.pdf", pdf_bytes, "application/pdf")],
    )
except RuntimeError:
    ...   # not configured / invalid key JSON  -> treat as "email is not set up"
except GmailApiError as exc:
    ...   # Gmail rejected it; exc.message is Google's error text (quota, bad recipient, delegation not granted...)
```

Patterns the original project uses on top of it (all in the services layer, not in the client):
- **User-facing mail (patient email with report):** failure is recorded (status `failed_to_submit` +
  error text) in a `cc_mail_transmissions` table, so staff can see it failed.
- **Account emails (password reset, temporary password):** failures are logged and swallowed so the
  action itself still succeeds and the endpoint does not reveal whether an account exists.
- **Plain-text bodies** are converted to HTML first (HTML-escape, then replace `\n` with `<br>`).
- **Fax-by-email:** the same function sends to `{digits}@westfax.com` (WestFax email-to-fax gateway); the
  client is generic, the destination address is the only difference.
- **Tests** monkeypatch `send_email` in the calling module (e.g. `monkeypatch.setattr(module,
  "send_email", fake)`), so no test ever contacts Google.

## 7. Behaviours and caveats worth knowing (accurate to the current code)

1. **A new access token is requested on every send** (no caching). This costs one extra HTTPS round-trip
   to Google's token endpoint per email. Fine for low volume; cache the token until ~expiry (`credentials.expiry`)
   if you send in bulk.
2. **Blocking I/O.** `send_email` is synchronous. In an async framework run it in a thread pool or a
   background task.
3. **Attachment content type is ignored.** `MIMEApplication(attachment.content)` sets
   `application/octet-stream`; the dataclass's `content_type` field is never applied. Recipients still
   open PDFs by filename extension, but use `MIMEApplication(content, _subtype="pdf")` (or `MIMEBase`
   with the given type) if the correct MIME type matters.
4. **Size.** Gmail messages are capped at ~25 MB, and base64 inflates attachments by ~33%, so keep total
   attachments under ~18 MB. Large files should be linked, not attached.
5. **From address is fixed** to the delegated mailbox (the display name is a code constant). Reply-To
   is not set; replies go to the sender mailbox.
6. **Sent mail appears in the sender mailbox's Sent folder** (it is a real send as that user).
7. **`gmail.send` only.** The service account cannot read the mailbox, so bounces/replies must be
   handled by reading that mailbox separately (the project does not do this).
8. **A latent logging bug in the original project** (`app/services/users.py`):
   `logger.exception("Account setup email delivery failed", to_email)` passes an argument with no `%s`
   placeholder, so Python's logging prints a formatting error instead of the message. If you copy that
   pattern, log an id instead (e.g. `"... for user_id=%s", user_id`) and avoid logging email addresses.
9. **Typical failure messages:** `unauthorized_client` / "Client is unauthorized to retrieve access
   tokens using this method" = domain-wide delegation not set up or wrong Client ID/scope;
   "Delegation denied" = sender mailbox not on the Workspace domain; 403 `rateLimitExceeded` / daily
   limit = Gmail sending quota of the sender mailbox.

## 8. Minimal checklist to reproduce in another app

- [ ] Gmail API enabled in a GCP project; service account + JSON key created.
- [ ] Domain-wide delegation granted (Client ID + `gmail.send` scope) in Workspace Admin.
- [ ] Real Workspace mailbox chosen as sender.
- [ ] Two config values: `GMAIL_SENDER_EMAIL`, `GMAIL_SERVICE_ACCOUNT_JSON` (single-line JSON), kept in
      `.env` locally (git-ignored) and in a secret manager in production; never logged or committed.
- [ ] Dependencies: `google-auth`, `requests`, `httpx`.
- [ ] Add the module from section 5; call `send_email(...)` from your service layer and handle
      `RuntimeError` (not configured) and `GmailApiError` (Gmail refused).
- [ ] Tests monkeypatch `send_email`; do one real send to yourself to verify delegation works.
