"""End-to-end smoke test against a running app and scripts/fake_upstream.py.

    python scripts/smoke.py --app http://127.0.0.1:8000 --fake http://127.0.0.1:9100 \
        --secret dev-webhook-secret

Standard library only. Exits non-zero on the first failed check.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


def call(method: str, url: str, body: object | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            status, raw = res.status, res.read().decode()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read().decode()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def check(condition: bool, label: str) -> None:
    print(("PASS " if condition else "FAIL ") + label, flush=True)
    if not condition:
        sys.exit(1)


def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def emails_to(fake: str, address: str) -> list[dict]:
    _, data = call("GET", f"{fake}/_emails")
    return [e for e in data["emails"] if address in (e["to"] if isinstance(e["to"], list) else [e["to"]])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="http://127.0.0.1:8000")
    parser.add_argument("--fake", default="http://127.0.0.1:9100")
    parser.add_argument("--secret", default="dev-webhook-secret")
    parser.add_argument("--sku", default="SMOKE-" + uuid.uuid4().hex[:6].upper())
    args = parser.parse_args()
    app, fake, sku = args.app.rstrip("/"), args.fake.rstrip("/"), args.sku
    customer = f"smoke+{uuid.uuid4().hex[:6]}@example.com"

    check(wait_for(lambda: call("GET", f"{app}/healthz")[0] == 200), "healthz is up")
    call("POST", f"{fake}/_control/stock", {"sku": sku, "quantity": 0})

    status, body = call("POST", f"{app}/api/requests", {"email": customer.upper(), "sku": sku.lower()})
    check(status == 201, f"new request -> 201 ({status})")
    status, body = call("POST", f"{app}/api/requests", {"email": customer, "sku": sku})
    check(status == 200 and body.get("already_subscribed") is True, "duplicate request -> 200 already_subscribed")

    status, _ = call("POST", f"{app}/webhooks/stock", {"event_id": "x", "sku": sku, "quantity": 3},
                     {"X-Webhook-Secret": "wrong"})
    check(status == 401, "webhook with wrong secret -> 401")

    phantom_id = "evt_" + uuid.uuid4().hex
    status, body = call("POST", f"{app}/webhooks/stock",
                        {"event_id": phantom_id, "type": "stock.updated", "sku": sku,
                         "quantity": 12, "occurred_at": "2026-11-08T12:00:00Z"},
                        {"X-Webhook-Secret": args.secret})
    check(status == 200 and body.get("outcome") == "phantom", f"phantom webhook ignored ({body})")

    call("POST", f"{fake}/_control/stock", {"sku": sku, "quantity": 12})
    call("POST", f"{fake}/_control/fail_emails", {"count": 1})  # first send fails with 500
    event_id = "evt_" + uuid.uuid4().hex
    event = {"event_id": event_id, "type": "stock.updated", "sku": sku, "quantity": 12,
             "occurred_at": "2026-11-08T12:00:00Z"}
    status, body = call("POST", f"{app}/webhooks/stock", event, {"X-Webhook-Secret": args.secret})
    check(status == 200 and body.get("outcome") == "verified", f"real restock verified ({body})")
    status, body = call("POST", f"{app}/webhooks/stock", event, {"X-Webhook-Secret": args.secret})
    check(body.get("status") == "duplicate", "redelivered event -> duplicate")

    check(wait_for(lambda: len(emails_to(fake, customer)) >= 1, timeout=30),
          "email delivered after a failed first attempt")
    time.sleep(2)
    sent = emails_to(fake, customer)
    check(len(sent) == 1, f"exactly one email to the customer ({len(sent)})")
    check(sent[0]["subject"].endswith("is back in stock"), "subject looks right")
    check(sent[0]["idempotency_key"].startswith("bis-request-"), "stable idempotency key used")

    token = base64.b64encode(f"admin:{args.secret}".encode()).decode()
    status, _ = call("GET", f"{app}/admin")
    check(status == 401, "admin without password -> 401")
    status, page = call("GET", f"{app}/admin?q={sku}", headers={"Authorization": f"Basic {token}"})
    check(status == 200 and customer in page and "Notified" in page, "admin lists the notified request")

    print("SMOKE OK", flush=True)


if __name__ == "__main__":
    main()
