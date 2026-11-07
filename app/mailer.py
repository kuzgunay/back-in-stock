"""Sends email through a Resend-compatible API."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings

# Worth retrying later: provider/network trouble, rate limits, a request with
# the same Idempotency-Key still in flight (409), or credentials an operator
# can fix (401/403). Any other 4xx means this exact message will never be
# accepted, so retrying would only hammer the API.
_TRANSIENT_STATUS = {401, 403, 408, 409, 425, 429}


@dataclass(frozen=True)
class SendResult:
    ok: bool
    email_id: str | None = None
    error: str | None = None
    permanent: bool = False


class Mailer:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._url = settings.resend_base_url.rstrip("/") + "/emails"
        self._from = settings.email_from
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(settings.email_timeout, connect=min(3.0, settings.email_timeout)),
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Accept": "application/json",
                "User-Agent": "back-in-stock/1.0",
            },
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(
        self, *, to: str, subject: str, html: str, text: str, idempotency_key: str
    ) -> SendResult:
        payload = {"from": self._from, "to": [to], "subject": subject, "html": html, "text": text}
        try:
            response = await self._client.post(
                self._url, json=payload, headers={"Idempotency-Key": idempotency_key}
            )
        except httpx.HTTPError as exc:
            # Includes timeouts after the provider may already have accepted the
            # message; the retry reuses the Idempotency-Key, so it cannot double-send.
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}".rstrip(": "))

        if 200 <= response.status_code < 300:
            email_id = None
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("id") is not None:
                    email_id = str(body["id"])[:200]
            except ValueError:
                pass
            return SendResult(ok=True, email_id=email_id)

        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or "")[:200]
        except ValueError:
            pass
        error = f"email API returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
        status = response.status_code
        permanent = 400 <= status < 500 and status not in _TRANSIENT_STATUS
        return SendResult(ok=False, error=error, permanent=permanent)
