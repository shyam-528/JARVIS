# JARVIS public web application

The public website is a separate FastAPI application in `api/public.py`, with
its frontend in `web/public/`. It provides accounts, sign-in/out, private chat
history, conversation deletion, and usage limits. It does not start or import
the desktop orchestrator. The existing desktop app is unchanged.

## Run locally (PowerShell)

From the repository root:

```powershell
.venv/Scripts/python.exe -m pip install -r requirements.web.txt
$env:JARVIS_WEB_ORIGIN = 'http://localhost:8090'
$env:JARVIS_WEB_SECURE_COOKIES = '0'
# Set these through your shell or secret manager; never commit actual values.
$env:JARVIS_WEB_PROVIDER_URL = 'https://your-provider.example/v1'
$env:JARVIS_WEB_PROVIDER_KEY = '<your server-side key>'
$env:JARVIS_WEB_MODEL = '<model supported by that provider>'
.venv/Scripts/python.exe -m uvicorn api.public:app --host 127.0.0.1 --port 8090
```

Open **http://localhost:8090** (use that exact hostname). Without provider
credentials, signup/login/history still work and the UI displays a setup notice.
Chat returns a clear 503 without calling a provider or consuming daily quota.
The desktop `.env` is deliberately not loaded; use `.env.web.example` as the
list of supported environment variables, not as live credentials.

## What is isolated

- SQLite records have user ownership, checked on every chat read, send, and delete.
- Context comes only from the authenticated user's selected conversation.
- Passwords are salted and hashed with scrypt. Random session tokens are hashed
  in the database and sent through HttpOnly, SameSite=Strict cookies, expiring
  after seven days. Logout revokes the current session.
- Mutating requests require the exact configured Origin and JSON content type.
- The service exposes no desktop tools, shared memory, mail, WhatsApp, Telegram,
  filesystem, shell, vision, mesh, or broadcast WebSocket routes.
- The browser renders generated content as text, not HTML. Chat responses are
  complete replies (not streaming). There are no external frontend dependencies.
- A dedicated provider gets conversation text to answer questions. Account privacy
  is not end-to-end encryption: the hosting administrator and AI provider process
  this data. Do not promise otherwise in a privacy policy.

## Deployment

Deploy **`api.public:app`**, never `api.main:app`, as the internet-facing service.
Install only `requirements.web.txt` on a clean Linux or Windows host. Do not copy
the desktop `data/`, `.env`, WhatsApp auth files, or personal documents to the host.
Use a fresh, dedicated AI key and private persistent database volume.

1. Set `JARVIS_WEB_ORIGIN` to the exact HTTPS origin (no path or trailing slash).
2. Keep `JARVIS_WEB_SECURE_COOKIES=1` (the default).
3. Set the provider URL/key/model and an absolute `JARVIS_WEB_DB` on persistent storage.
4. Place uvicorn behind a TLS reverse proxy. Preserve the external Host header;
   configure uvicorn's `--forwarded-allow-ips` to trust only that proxy's address.
   Never trust arbitrary forwarded client IPs from the internet.
5. Publish only this service. Keep the desktop API (8080), mesh, and bridge private.
6. Back up the SQLite database using SQLite's backup API or a stopped service;
   copying only a live `.db` can miss WAL transactions.

Example server process behind a local reverse proxy:

```sh
python -m uvicorn api.public:app --host 127.0.0.1 --port 8090 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

Limits: 20 auth attempts per client IP per 15-minute window; 100 conversation
creations/account/day; 10 chat attempts/account/minute; default 100 chat
attempts/account/day (configurable). Daily windows reset at midnight UTC.
Provider failures consume an attempted-message allowance to prevent retry abuse.
Each conversation permits one reply in flight, with a recoverable lease after
worker crashes. Messages are stored as complete user/assistant pairs only.
Recent whole turns are sent within a character budget; older messages stay in
history but may be omitted from AI context. Replies are capped at 2,048 tokens.

SQLite suits an initial single-host deployment. For multiple application hosts,
migrate storage and atomic rate limits to shared services such as PostgreSQL
and Redis. Do not put SQLite on an unreliable network filesystem.

## Verification

No provider credentials or network access are required for these tests:

```powershell
.venv/Scripts/python.exe -m unittest tests.test_public_web -v
node --check web/public/app.js
```

Tests cover account isolation, session hashing/revocation/expiry, origin checks,
request size and validation, quotas, overlapping sends, deletion, missing-provider
behavior, upstream error redaction, and the absence of desktop API routes.

## Remaining work before a broad public launch

This is an initial multi-user chat implementation, not a completed commercial
service. Email verification, password reset, account export/deletion, billing,
moderation/abuse controls, operational alerting, and a reviewed privacy/retention
policy are not implemented. Limit access to a pilot until those are addressed;
`JARVIS_WEB_SIGNUP=0` closes new registrations while preserving existing logins.
No live hosting or domain deployment is performed by these code changes.
