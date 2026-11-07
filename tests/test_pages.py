"""HTML pages: admin auth, embed framing headers, escaping, no-JS fallback."""

from __future__ import annotations

import base64

from .conftest import WEBHOOK_SECRET, running

XSS = "<script>alert(1)</script>"


def basic(password: str, user: str = "admin") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def test_admin_requires_password(h):
    anon = await h.client.get("/admin")
    assert anon.status_code == 401
    assert anon.headers["www-authenticate"].startswith("Basic ")
    assert (await h.client.get("/admin", headers=basic("wrong"))).status_code == 401
    assert (await h.client.get("/admin", headers={"Authorization": "Basic !!!"})).status_code == 401
    assert (await h.client.get("/admin", headers={"Authorization": "Bearer x"})).status_code == 401
    # No ADMIN_PASSWORD configured: falls back to WEBHOOK_SECRET; any username works.
    ok = await h.client.get("/admin", headers=basic(WEBHOOK_SECRET, user="anyone"))
    assert ok.status_code == 200
    assert "Restock requests" in ok.text


async def test_admin_password_overrides_fallback(settings, stock, resend, clock):
    async with running(settings, stock, resend, clock, admin_password="s3cret-admin") as h:
        assert (await h.client.get("/admin", headers=basic(WEBHOOK_SECRET))).status_code == 401
        assert (await h.client.get("/admin", headers=basic("s3cret-admin"))).status_code == 200


async def test_admin_cannot_be_framed_or_cached(h):
    res = await h.client.get("/admin", headers=basic(WEBHOOK_SECRET))
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "no-store"


async def test_admin_shows_requests_and_escapes_everything(h, stock, resend):
    await h.subscribe("a@example.com", "ABC-1")
    await h.subscribe("b@example.com", "XYZ-2")
    await h.drain()
    stock.set("ABC-1", 3)
    await h.webhook(XSS, "ABC-1")
    await h.webhook("evt_phantom", "XYZ-2")
    await h.webhook("evt_phantom", "XYZ-2")
    await h.drain()

    page = (await h.client.get("/admin", headers=basic(WEBHOOK_SECRET))).text
    assert XSS not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "a@example.com" in page and "b@example.com" in page
    assert "Notified" in page and "Waiting" in page
    assert "Phantom" in page and "Verified restock" in page
    assert "+1 duplicate" in page

    search = (await h.client.get("/admin?q=XYZ", headers=basic(WEBHOOK_SECRET))).text
    assert "b@example.com" in search and "a@example.com" not in search
    notified = (await h.client.get("/admin?status=notified", headers=basic(WEBHOOK_SECRET))).text
    assert "a@example.com" in notified and "b@example.com" not in notified
    injected = await h.client.get(f"/admin?q={XSS}&status={XSS}", headers=basic(WEBHOOK_SECRET))
    assert injected.status_code == 200 and XSS not in injected.text


async def test_embed_is_frameable_anywhere(h):
    res = await h.client.get("/embed")
    assert res.status_code == 200
    assert "x-frame-options" not in res.headers
    csp = res.headers["content-security-policy"]
    assert "frame-ancestors *" in csp
    assert "set-cookie" not in res.headers
    assert 'type="email"' in res.text and 'for="bis-email"' in res.text
    assert 'name="sku"' in res.text and 'type="text"' in res.text  # SKU field visible


async def test_embed_prefills_and_hides_sku_and_escapes_product(h):
    res = await h.client.get("/embed", params={"sku": "abc-1", "product": XSS,
                                               "accent": "red;}</style><script>"})
    html = res.text
    assert '<input type="hidden" name="sku" id="bis-sku" value="ABC-1">' in html
    assert XSS not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "</style><script>" not in html
    assert "--accent: #18181b" in html  # invalid accent ignored
    themed = await h.client.get("/embed", params={"accent": "#2563EB", "theme": "dark"})
    assert "--accent: #2563eb" in themed.text and 'data-theme="dark"' in themed.text


async def test_embed_scripts_are_nonced(h):
    res = await h.client.get("/embed")
    csp = res.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in res.text
    assert "unsafe-inline" not in csp


async def test_embed_works_without_javascript(h):
    form = {"email": "nojs@example.com", "sku": "ABC-1"}
    created = await h.client.post("/embed?sku=ABC-1", data=form)
    assert created.status_code == 201
    assert "You&rsquo;re on the list" in created.text
    again = await h.client.post("/embed?sku=ABC-1", data=form)
    assert again.status_code == 200
    assert "already on the list" in again.text
    bad = await h.client.post("/embed", data={"email": "nope", "sku": "ABC-1"})
    assert bad.status_code == 422
    assert "valid email" in bad.text
    assert len(await h.requests()) == 1


async def test_health_and_landing(h):
    assert (await h.client.get("/healthz")).json() == {"ok": True}
    landing = await h.client.get("/")
    assert landing.status_code == 200
    assert 'href="/embed"' in landing.text
    assert landing.headers["x-frame-options"] == "DENY"


async def test_json_responses_have_safe_defaults(h):
    res = await h.client.get("/healthz")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
