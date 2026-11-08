# Back in Stock

Customers ask to be emailed when a sold-out item comes back; this service makes sure each of them hears about it **exactly once**, and only when the item really is back.

- **`GET /embed`**: a signup form built to live in an iframe on any storefront.
- **`POST /api/requests`**: the same thing as JSON (CORS enabled).
- **`POST /webhooks/stock`**: stock events, verified against the stock API before anyone is emailed.
- **`GET /admin`**: a password-protected dashboard showing every request, its status and when it was notified, plus recent webhooks and what came of them.
- **Email**: responsive HTML with a plain-text part, sent through the Resend API format.

Python 3.12, FastAPI, httpx, SQLite. No other services are needed.

## Run it

### Docker

```bash
docker build -t back-in-stock .
docker run -p 8000:8000 \
  -e PORT=8000 \
  -e STOCK_API_URL=https://stock.example.com/products \
  -e STOCK_API_KEY=... \
  -e RESEND_API_KEY=... \
  -e RESEND_BASE_URL=https://api.resend.com \
  -e WEBHOOK_SECRET=... \
  -e ADMIN_PASSWORD=... \
  back-in-stock
```

Data lives in the `/data` volume. Add `-v bis-data:/data` to keep it when the container is re-created; a plain `docker restart` keeps it anyway. The image runs as a non-root user and has a `HEALTHCHECK` on `/healthz`.

### Locally (Python 3.12+)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill it in and export the variables
python -m app          # same as: uvicorn app.main:create_app --factory --workers 1
```

To try it end to end without real services, use the bundled fake stock and email API:

```bash
python scripts/fake_upstream.py --port 9100 &
STOCK_API_URL=http://127.0.0.1:9100/products STOCK_API_KEY=test-stock-key \
RESEND_API_KEY=test-resend-key RESEND_BASE_URL=http://127.0.0.1:9100 \
WEBHOOK_SECRET=dev-webhook-secret PORT=8000 python -m app &
python scripts/smoke.py --app http://127.0.0.1:8000 --fake http://127.0.0.1:9100 --secret dev-webhook-secret
```

`http://127.0.0.1:9100/_emails/latest.html` shows the last email the fake received.

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `STOCK_API_URL` | yes | | `GET {url}/{sku}` and `GET {url}` (catalogue) |
| `STOCK_API_KEY` | yes | | Sent as `Authorization: Bearer …` |
| `RESEND_API_KEY` | yes | | Sent as `Authorization: Bearer …` |
| `RESEND_BASE_URL` | yes | | Emails go to `POST {url}/emails` |
| `WEBHOOK_SECRET` | yes | | Expected in `X-Webhook-Secret` |
| `PORT` | yes | `8000` | Port to listen on |
| `ADMIN_PASSWORD` | no | `WEBHOOK_SECRET` | `/admin` password (HTTP Basic, any username) |
| `DATA_DIR` | no | `./data` (`/data` in Docker) | SQLite file is `$DATA_DIR/app.db` |
| `EMAIL_FROM` | no | `Back in Stock <alerts@backinstock.example>` | |
| `STORE_NAME` | no | `Back in Stock` | Shown in the email header |
| `STORE_URL` | no | | Target of the email's "Shop it now" button (a product `url` from the stock API wins) |
| `RECONCILE_INTERVAL_SECONDS` | no | `60` | How often to look for restocks whose webhook never arrived |
| `LOG_LEVEL` | no | `INFO` | JSON logs on stdout; secrets are never logged and emails are masked |

`ADMIN_PASSWORD` protects `/admin` (HTTP Basic, any username), as in the brief's env list. If it is not set, the admin password falls back to `WEBHOOK_SECRET`, so `/admin` is never left open.

## How duplicate and missed emails are prevented

Every restock signal (a webhook, a new signup for an item that's already in stock, or the periodic reconciler) goes through the same pipeline, and all of its state is in SQLite:

```
signal ──> stock check (queued in DB) ──> stock API says in stock? ──> claim open requests ──> send ──> notified
                                              └─ no: nothing happens        open -> notifying     (retried until accepted)
```

**No duplicate requests.** Emails are lower-cased and SKUs upper-cased, and a partial unique index on `(email, sku) WHERE status IN ('open','notifying')` makes a second open request impossible, even with concurrent inserts. A duplicate gets `200 {"already_subscribed": true}` along with the existing request. Once a customer has been notified they can sign up again.

**Don't trust the webhook.** The webhook's `quantity` is treated as a hint. Every new event is checked with `GET {STOCK_API_URL}/{sku}`, and customers are only emailed if the stock API confirms stock. If it doesn't, the event is recorded as *phantom* in the admin.

**Duplicate and concurrent deliveries.** `event_id` is the primary key of `webhook_events`. The insert (`ON CONFLICT DO NOTHING`) and the queued stock check are committed in one transaction, so of two identical deliveries arriving at the same moment, exactly one does the work and the other gets `200 {"status": "duplicate"}`.

**Exactly one email per request.**
1. *Claim.* `UPDATE requests SET status='notifying' … WHERE id=? AND status='open'` moves a row out of `open`, and only one caller can ever do that. Two webhooks for the same SKU, or a webhook racing the reconciler, can't both claim the same customer.
2. *Send.* Only the worker sends, and only `notifying` rows. Each send carries `Idempotency-Key: bis-<install-id>-<request-id>`, which stays the same across retries and restarts. The install id is a random value stored in the database itself, so a fresh database can never collide with a key an earlier one already used (otherwise the provider would silently de-duplicate a brand-new customer's email). The email is rendered from a product snapshot stored at claim time, so every retry sends an identical payload.
3. *Record.* A 2xx response sets `notified`, `notified_at` and the provider's `email_id`. A 5xx, 429, timeout or network error keeps the row in `notifying` and schedules a retry with exponential backoff (5 s doubling up to a 15-minute cap, with jitter). The attempt count and last error show in the admin. A permanent rejection (400/422) marks the row `failed`, so a bad address doesn't get retried forever.
4. *Crash between send and record.* The row is still `notifying`, so it is sent again after the restart with the same Idempotency-Key, and the provider returns the original result instead of sending a second email. The same applies when a request times out after the provider has already accepted it.

Once a restock is verified, a `notifying` row is retried **without** checking stock again. Stock was confirmed when the row was claimed; checking again on every retry could strand a customer who should hear about the restock if the item sold out between a failed attempt and its retry.

**Don't trust the network.** Every outbound call has a timeout (5 s for the stock API, 10 s for email). If the stock API fails while a webhook is being handled, the stock check stays queued in the database with backoff and the webhook gets `202`. A new signup never fails because the stock API is down; its stock check is simply queued.

**Missed webhooks.** Every `RECONCILE_INTERVAL_SECONDS` (and shortly after startup), the reconciler takes the SKUs that customers are waiting for, fetches the catalogue once (`GET {STOCK_API_URL}`), and claims requests for anything that is in stock. When nobody is waiting it makes no calls at all; if the catalogue call fails it falls back to a few per-SKU lookups.

**Restarts.** Requests, events, queued stock checks, retry schedules and in-flight markers are all in SQLite (WAL mode, `synchronous=FULL`). On startup the worker clears leases left by the old process and resumes everything that is due.

### Why one process

The app runs with exactly one uvicorn worker, on purpose. The delivery worker and the reconciler are asyncio tasks inside that process, and SQLite works best with a single writer. Releasing in-flight leases at startup is only safe because there is exactly one worker. Correctness doesn't depend on in-memory state, though: duplicates are blocked by database constraints and conditional updates, which hold across processes. Running several instances would need a shared database (Postgres) and lease expiry in place of release-at-startup.

## Endpoints

| | |
|---|---|
| `GET /` | Landing page with a live preview and an embed snippet |
| `GET /embed` | Signup form. Query parameters: `sku` (prefills and hides the SKU field), `product` (label), `accent` (hex colour), `theme` (`light`/`dark`/`auto`). Sends `Content-Security-Policy: frame-ancestors *` and no `X-Frame-Options`. No cookies. It posts a `back-in-stock:resize` message to the parent window so the host page can size the iframe. Without JavaScript, the form still works as a normal form post. |
| `POST /api/requests` | `{"email", "sku"}`. Returns `201` when created, `200` + `already_subscribed` for a duplicate, `422` with per-field errors, `400` for invalid JSON. |
| `POST /webhooks/stock` | Needs `X-Webhook-Secret` (compared in constant time, `401` otherwise). Returns `200` processed/duplicate, or `202` when stock verification is queued. |
| `GET /admin` | HTTP Basic auth. Summary counts, filter by status, search by email or SKU, attempts and last error, recent webhooks with their outcome (verified, phantom, sold out, verifying) and duplicate counts. |
| `GET /healthz` | `{"ok": true}` |

Security: all HTML output is auto-escaped, pages use a nonce-based CSP, the admin page can't be framed or cached, request bodies are capped at 16 KB, and only `/api/*` sends CORS headers.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite runs against in-process fakes of the stock API and Resend (`httpx.MockTransport`) and makes no network calls. It covers: case-insensitive and concurrent duplicate signups; wrong or missing webhook secret; phantom restocks; the same `event_id` delivered twice at once; several events for one SKU at once; email 500s followed by a later retry with a stable Idempotency-Key; a lost provider response; a restart with a pending send; a crash between send and DB update; the stock API being down during a webhook; an item already in stock at signup; the reconciler catching a missed webhook; admin auth; the embed's framing headers; and XSS escaping.

`scripts/smoke.py` runs the same scenario against a live server. CI (`.github/workflows/docker.yml`) runs the tests on Python 3.12 and 3.14, builds the image, runs it with only the documented env vars next to the fake upstream, runs the smoke test, restarts the container to check the data survived, and checks that it runs as non-root and becomes healthy.

## Trade-offs and limits

- One instance, SQLite (see above). This fits the brief and is the right size for one store; scaling out means Postgres.
- Transient email failures are retried indefinitely (capped at 15 minutes) rather than abandoned, because hearing zero times is the worse outcome. Resend keeps idempotency keys for 24 hours, so a duplicate would only be possible if the provider accepted a send, the response was lost, and the provider then stayed unreachable for a day.
- The email links to a product `url` if the stock API provides one, otherwise to `STORE_URL`. Without either, the button is left out.
- Numeric prices are shown in dollars. Price strings are shown as given.
- There is no rate limiting on the public endpoint (the test harness posts server-to-server from one IP); put it behind a proxy or CDN rule in production. Serve `/admin` over HTTPS.

## AI tools & time

- **Tool:** Claude Code (Anthropic), for design, implementation, tests and review. I directed the work, reviewed the output and made the deployment decisions.
- **Time:** about 70 minutes end to end.
  - ~40 min: building it and writing the test suite.
  - ~30 min: fixing three CI issues, deploying to Railway with a persistent volume, and checking it against the test server.
- **Checked against the test server**, with an injected 500 and a manual restock that sent no webhook:
  - phantom restocks ignored;
  - duplicate deliveries ignored;
  - the failed email retried and delivered once;
  - the silent restock caught by the reconciler;
  - data intact after a redeploy.

Live: https://back-in-stock-production-d78b.up.railway.app (`/embed`, `/admin`).
