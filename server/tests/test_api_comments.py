# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 1: 公開プレイヤー島 コメント/pin API。投稿認証(session+CSRF)・クールダウン・所有権。"""

import json

from django.test import Client
from django.utils import timezone


def _member(*, verified=True, email="u@example.com", nickname="ニック"):
    from members.models import Member

    m = Member(
        email=email,
        nickname=nickname,
        birth_year=1990,
        birth_month=5,
        postal_code="1500001",
        email_verified_at=timezone.now() if verified else None,
    )
    m.set_password("pw123456")  # pragma: allowlist secret - test only
    m.save()
    return m


def _login(client, member):
    s = client.session
    s["member_id"] = member.pk
    s.save()


def _post(client, slug, body):
    return client.post(
        f"/api/v1/channels/{slug}/comments",
        data=json.dumps({"body": body}),
        content_type="application/json",
    )


def test_list_empty_gate_login(channel):
    d = Client().get("/api/v1/channels/ch1/comments").json()
    assert d["gate"] == "login"
    assert d["can_post"] is False
    assert d["items"] == []
    assert d["me_member_id"] is None


def test_post_requires_login(channel):
    assert _post(Client(), "ch1", "hi").status_code == 401


def test_post_unverified_403(channel):
    c = Client()
    _login(c, _member(verified=False))
    assert _post(c, "ch1", "hi").status_code == 403


def test_post_and_list(channel):
    c = Client()
    m = _member()
    _login(c, m)
    resp = _post(c, "ch1", "こんにちは")
    assert resp.status_code == 200, resp.content
    item = resp.json()
    assert item["body"] == "こんにちは"
    assert item["member_id"] == m.pk
    lst = c.get("/api/v1/channels/ch1/comments").json()
    assert lst["count"] == 1
    assert lst["gate"] == "ok"
    assert lst["me_member_id"] == m.pk
    assert lst["items"][0]["body"] == "こんにちは"


def test_post_empty_400(channel):
    c = Client()
    _login(c, _member())
    assert _post(c, "ch1", "   ").status_code == 400


def test_post_cooldown_429(channel):
    c = Client()
    _login(c, _member())
    assert _post(c, "ch1", "one").status_code == 200
    assert _post(c, "ch1", "two").status_code == 429


def test_delete_own_and_other(channel):
    ca = Client()
    a = _member(email="a@example.com", nickname="A")
    _login(ca, a)
    cid = _post(ca, "ch1", "mine").json()["id"]
    cb = Client()
    _login(cb, _member(email="b@example.com", nickname="B"))
    assert cb.delete(f"/api/v1/channels/ch1/comments/{cid}").status_code == 403
    assert ca.delete(f"/api/v1/channels/ch1/comments/{cid}").status_code == 200
    assert ca.get("/api/v1/channels/ch1/comments").json()["count"] == 0


def test_pin_toggle(channel):
    c = Client()
    _login(c, _member())
    r1 = c.post("/api/v1/channels/ch1/pin")
    assert r1.status_code == 200 and r1.json()["pinned"] is True
    assert c.post("/api/v1/channels/ch1/pin").json()["pinned"] is False


def test_csrf_enforced_on_post(channel):
    c = Client(enforce_csrf_checks=True)
    _login(c, _member())
    # CSRF トークン無しの会員 POST は拒否 (session cookie だけでは通さない)
    assert _post(c, "ch1", "hi").status_code == 403
