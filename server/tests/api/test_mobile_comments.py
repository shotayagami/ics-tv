# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""アプリ (Bearer トークン) からのコメント投稿・削除・ピン留め (#MOBILE-01)。

comments/pin は元々 session cookie 専用の `member_auth` だったため、アプリの Bearer トークンは
CSRF チェックに阻まれて弾かれていた (CSRF は request.member の由来を問わず、Cookie 認証の
非 GET すべてに一律で要求される)。`member_any_auth` へ切り替えた後、実際に CSRF なしで
通ることを確認する。

既定の `http_client` フィクスチャ (`Client()`) は `enforce_csrf_checks=False` で CSRF を
無効化しており、この種の不具合を素通りさせる (api/test_mobile_auth.py の CSRF 回帰テストと
同じ罠)。ここでは全テストで `Client(enforce_csrf_checks=True)` を明示的に使う。
"""

from __future__ import annotations

from django.test import Client, override_settings
from django.utils import timezone

from members.models import ChannelFavorite, Comment, Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", verified=True, **over):
    m = Member(
        email=email,
        nickname="みんと",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        **over,
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _token_for(email="m@example.com") -> str:
    """CSRF を強制するクライアントで /auth/login を叩いてトークンを取る。

    ログイン自体は auth=None (未認証) なので、この呼び出しに CSRF は要らない。
    """
    csrf_client = Client(enforce_csrf_checks=True)
    res = csrf_client.post(
        "/api/v1/auth/login",
        {"email": email, "password": _PW},
        content_type="application/json",
    )
    return res.json()["token"]


def _auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@_PUBLIC_HOST
def test_post_comment_with_bearer_token_needs_no_csrf(channel, db):
    _member()
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.post(
        f"/api/v1/channels/{channel.slug}/comments",
        {"body": "こんにちは"},
        content_type="application/json",
        **_auth(token),
    )

    assert res.status_code == 200
    assert Comment.objects.get().body == "こんにちは"


@_PUBLIC_HOST
def test_post_comment_with_bearer_but_unverified_member_is_403(channel, db):
    _member(verified=False)
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.post(
        f"/api/v1/channels/{channel.slug}/comments",
        {"body": "x"},
        content_type="application/json",
        **_auth(token),
    )

    assert res.status_code == 403
    assert not Comment.objects.exists()


@_PUBLIC_HOST
def test_delete_own_comment_with_bearer_token_needs_no_csrf(channel, db):
    m = _member()
    token = _token_for()
    comment = Comment.objects.create(channel=channel, member=m, body="消す")
    client = Client(enforce_csrf_checks=True)

    res = client.delete(f"/api/v1/channels/{channel.slug}/comments/{comment.id}", **_auth(token))

    assert res.status_code == 200
    comment.refresh_from_db()
    assert comment.deleted_at is not None


@_PUBLIC_HOST
def test_cannot_delete_others_comment_with_bearer_token(channel, db):
    other = _member("other@example.com")
    _member("me@example.com")
    token = _token_for("me@example.com")
    comment = Comment.objects.create(channel=channel, member=other, body="他人の")
    client = Client(enforce_csrf_checks=True)

    res = client.delete(f"/api/v1/channels/{channel.slug}/comments/{comment.id}", **_auth(token))

    assert res.status_code == 403
    comment.refresh_from_db()
    assert comment.deleted_at is None


@_PUBLIC_HOST
def test_pin_toggle_with_bearer_token_needs_no_csrf(channel, db):
    _member()
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    on = client.post(f"/api/v1/channels/{channel.slug}/pin", **_auth(token))
    assert on.status_code == 200
    assert on.json()["pinned"] is True
    assert ChannelFavorite.objects.count() == 1

    off = client.post(f"/api/v1/channels/{channel.slug}/pin", **_auth(token))
    assert off.status_code == 200
    assert off.json()["pinned"] is False
    assert ChannelFavorite.objects.count() == 0


@_PUBLIC_HOST
def test_comment_endpoints_reject_a_garbage_token(channel, db):
    """本文投稿は起きない、が返るステータスは 403 (401 ではない) — 既知の粗さ。

    member_any_auth = [member_token_auth, member_auth] は、トークンが解決できないと
    session-cookie 認証側 (APIKeyCookie) へフォールバックする。そちらは session の有無を
    見るより先に、非 GET なら無条件で CSRF チェックを行う実装になっているため、
    「無効なトークン」も「CSRF cookie が無い」も同じ 403 として現れる。実害はない
    (投稿は成立しない) が、クライアントは 4xx を一律「要再ログイン」として扱うべきで、
    401 と 403 の違いに依存した分岐をしないこと。
    """
    client = Client(enforce_csrf_checks=True)

    res = client.post(
        f"/api/v1/channels/{channel.slug}/comments",
        {"body": "x"},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer not-a-real-token",
    )

    assert res.status_code == 403
    assert not Comment.objects.exists()
