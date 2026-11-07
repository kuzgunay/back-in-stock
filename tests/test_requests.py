"""POST /api/requests: validation, de-duplication, CORS, limits."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest


async def test_creates_request_with_normalized_fields(h):
    res = await h.subscribe("  Jane.Doe@Example.COM ", " abc-1 ")
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == "jane.doe@example.com"
    assert body["sku"] == "ABC-1"
    assert body["status"] == "open"
    assert body["already_subscribed"] is False


async def test_duplicate_with_different_case_returns_existing_request(h):
    first = await h.subscribe("jane@example.com", "abc-1")
    second = await h.subscribe("JANE@example.com", "ABC-1")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["already_subscribed"] is True
    assert second.json()["id"] == first.json()["id"]
    assert len(await h.requests()) == 1


async def test_concurrent_duplicate_posts_create_one_row(h):
    variants = ["sam@example.com", "SAM@example.com", "Sam@Example.com", " sam@example.com"]
    responses = await asyncio.gather(
        *(h.subscribe(variants[i % len(variants)], "abc-1" if i % 2 else "ABC-1") for i in range(12))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1
    assert codes.count(200) == 11
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(await h.requests()) == 1


async def test_same_email_different_skus_are_separate_requests(h):
    assert (await h.subscribe("a@example.com", "ABC-1")).status_code == 201
    assert (await h.subscribe("a@example.com", "ABC-2")).status_code == 201
    assert len(await h.requests()) == 2


def test_database_itself_rejects_a_second_open_request(settings, tmp_path):
    """The guarantee holds even for writers that bypass the app entirely."""
    from app.db import SCHEMA

    conn = sqlite3.connect(tmp_path / "raw.db")
    conn.executescript(SCHEMA)
    insert = "INSERT INTO requests (email, sku, status, created_at) VALUES (?, ?, ?, 'now')"
    conn.execute(insert, ("x@example.com", "ABC-1", "open"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("x@example.com", "ABC-1", "notifying"))
    # A notified request does not block a new subscription.
    conn.execute("UPDATE requests SET status = 'notified'")
    conn.execute(insert, ("x@example.com", "ABC-1", "open"))
    conn.close()


@pytest.mark.parametrize(
    "body, field",
    [
        ({"email": "not-an-email", "sku": "ABC-1"}, "email"),
        ({"email": "a@b", "sku": "ABC-1"}, "email"),
        ({"email": "", "sku": "ABC-1"}, "email"),
        ({"sku": "ABC-1"}, "email"),
        ({"email": "a@example.com", "sku": "bad sku!"}, "sku"),
        ({"email": "a@example.com", "sku": "x" * 65}, "sku"),
        ({"email": "a@example.com"}, "sku"),
        ({"email": "<script>@example.com", "sku": "ABC-1"}, "email"),
    ],
)
async def test_validation_errors_are_clear_json(h, body, field):
    res = await h.client.post("/api/requests", json=body)
    assert res.status_code == 422
    data = res.json()
    assert data["error"] == "validation_error"
    assert field in data["fields"]
    assert data["message"]
    assert await h.requests() == []


async def test_invalid_json_is_400(h):
    res = await h.client.post("/api/requests", content=b"{nope",
                              headers={"Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_json"


async def test_non_object_body_is_422(h):
    res = await h.client.post("/api/requests", json=["a@example.com", "ABC-1"])
    assert res.status_code == 422


async def test_cors_allows_any_origin(h):
    pre = await h.client.options("/api/requests", headers={
        "Origin": "https://some-store.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "*"
    assert "POST" in pre.headers["access-control-allow-methods"]
    res = await h.client.post("/api/requests", json={"email": "c@example.com", "sku": "A1"},
                              headers={"Origin": "https://some-store.example"})
    assert res.headers["access-control-allow-origin"] == "*"


async def test_admin_and_webhook_get_no_cors(h):
    res = await h.webhook("evt_x", "ABC-1", secret="wrong")
    assert "access-control-allow-origin" not in res.headers


async def test_oversized_body_is_rejected(h):
    res = await h.client.post("/api/requests", content=b"{" + b" " * 20000 + b"}",
                              headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert await h.requests() == []


async def test_stock_api_outage_does_not_fail_the_request(h, stock):
    stock.down = True
    res = await h.subscribe("d@example.com", "ABC-1")
    assert res.status_code == 201
    await h.drain()  # the queued stock check fails and is rescheduled
    assert (await h.requests())[0]["status"] == "open"
