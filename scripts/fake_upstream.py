"""A tiny fake of the stock API and the Resend API, for local runs and smoke tests.

Standard library only, so it runs anywhere (including CI next to the container).

    python scripts/fake_upstream.py --port 9100

Stock API  : GET  /products            -> {"products": [...]}
             GET  /products/{sku}      -> {sku, name, price, image, quantity, in_stock}
Resend API : POST /emails              (honours Idempotency-Key)
Control    : GET  /_emails             -> emails accepted so far
             GET  /_emails/latest.html -> HTML of the most recent email (preview)
             POST /_control/stock      {"sku": "ABC-1", "quantity": 5}
             POST /_control/fail_emails {"count": 2}     next N sends return 500
             POST /_control/stock_down  {"down": true}   stock API returns 503
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STOCK_KEY = "test-stock-key"
RESEND_KEY = "test-resend-key"

state_lock = threading.Lock()
products = {
    "ABC-1": {"sku": "ABC-1", "name": "Linen Overshirt · Sand", "price": 89.0,
              "image": "https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?w=1120",
              "quantity": 0, "in_stock": False},
    "DEMO-1": {"sku": "DEMO-1", "name": "Merino Crew · Charcoal", "price": 120.0,
               "image": None, "quantity": 0, "in_stock": False},
}
emails: list[dict] = []
attempts: list[dict] = []
by_key: dict[str, dict] = {}
control = {"fail_emails": 0, "stock_down": False}


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeUpstream/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # quieter output
        print("[fake]", self.command, self.path, "->", args[1] if len(args) > 1 else "")

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["_emails"]:
            with state_lock:
                return self._json(200, {"emails": emails, "attempts": len(attempts)})
        if parts == ["_emails", "latest.html"]:
            with state_lock:
                html = emails[-1]["html"] if emails else "<p>No emails yet.</p>"
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts and parts[0] == "products":
            if self.headers.get("Authorization") != f"Bearer {STOCK_KEY}":
                return self._json(401, {"error": "unauthorized"})
            with state_lock:
                if control["stock_down"]:
                    return self._json(503, {"error": "unavailable"})
                if len(parts) == 1:
                    return self._json(200, {"products": list(products.values())})
                sku = parts[1].upper()
                return self._json(200, products.get(sku) or {
                    "sku": sku, "name": None, "price": None, "image": None,
                    "quantity": 0, "in_stock": False})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path == "/emails":
            if self.headers.get("Authorization") != f"Bearer {RESEND_KEY}":
                return self._json(401, {"message": "invalid api key"})
            payload = self._body()
            key = self.headers.get("Idempotency-Key")
            with state_lock:
                attempts.append({"key": key, "to": payload.get("to")})
                if control["fail_emails"] > 0:
                    control["fail_emails"] -= 1
                    return self._json(500, {"message": "simulated failure"})
                if key and key in by_key:
                    return self._json(200, {"id": by_key[key]["id"]})
                record = {"id": f"email_{len(emails) + 1}", "idempotency_key": key, **payload}
                emails.append(record)
                if key:
                    by_key[key] = record
            return self._json(200, {"id": record["id"]})
        body = self._body()
        with state_lock:
            if path == "/_control/stock":
                sku = str(body["sku"]).upper()
                qty = int(body["quantity"])
                product = products.setdefault(sku, {"sku": sku, "name": sku, "price": 10.0,
                                                    "image": None})
                product.update(quantity=qty, in_stock=qty > 0)
                return self._json(200, product)
            if path == "/_control/fail_emails":
                control["fail_emails"] = int(body.get("count", 1))
                return self._json(200, control)
            if path == "/_control/stock_down":
                control["stock_down"] = bool(body.get("down", True))
                return self._json(200, control)
        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake stock + resend API on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
