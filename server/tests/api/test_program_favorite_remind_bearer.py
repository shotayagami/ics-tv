# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""番組詳細のお気に入り/リマインドをアプリの Bearer トークンで操作する (#MOBILE-01)。

既存の /members/favorites/,reminders/ のトグルビューは session cookie 専用の
@member_login_required のままで、アプリはこれを叩けなかった (icstv-android 側では
JSON API 自体が存在しないと判定し画面実装を見送っていた)。/api/v1/program/{id}/favorite
と /program/{id}/remind を member_any_auth (session or Bearer) で新設し、同じ
Favorite/Reminder モデルを共有する。ここでは comments/pin (#64) と同様、
Client(enforce_csrf_checks=True) で Bearer 経路が CSRF を要求しないことを確認する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from members.models import Favorite, Member, Reminder
from scheduling.models import Program, ProgramType

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
    csrf_client = Client(enforce_csrf_checks=True)
    res = csrf_client.post(
        "/api/v1/auth/login",
        {"email": email, "password": _PW},
        content_type="application/json",
    )
    return res.json()["token"]


def _auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _program(channel, asset, *, public=True, start_delta=timedelta(hours=3)):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組X",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        public_visible=public,
    )


@_PUBLIC_HOST
def test_favorite_toggle_with_bearer_token_needs_no_csrf(channel, asset_ready, db):
    _member()
    token = _token_for()
    p = _program(channel, asset_ready)
    client = Client(enforce_csrf_checks=True)

    on = client.post(f"/api/v1/program/{p.id}/favorite", **_auth(token))
    assert on.status_code == 200
    assert on.json()["favorited"] is True
    assert Favorite.objects.count() == 1

    off = client.post(f"/api/v1/program/{p.id}/favorite", **_auth(token))
    assert off.status_code == 200
    assert off.json()["favorited"] is False
    assert Favorite.objects.count() == 0


@_PUBLIC_HOST
def test_remind_toggle_with_bearer_token_needs_no_csrf(channel, asset_ready, db):
    _member()
    token = _token_for()
    p = _program(channel, asset_ready)
    client = Client(enforce_csrf_checks=True)

    on = client.post(f"/api/v1/program/{p.id}/remind", **_auth(token))
    assert on.status_code == 200
    assert on.json()["reminded"] is True
    assert Reminder.objects.count() == 1

    off = client.post(f"/api/v1/program/{p.id}/remind", **_auth(token))
    assert off.status_code == 200
    assert off.json()["reminded"] is False
    assert Reminder.objects.count() == 0


@_PUBLIC_HOST
def test_favorite_toggle_non_public_program_404(channel, asset_ready, db):
    _member()
    token = _token_for()
    p = _program(channel, asset_ready, public=False)
    client = Client(enforce_csrf_checks=True)

    res = client.post(f"/api/v1/program/{p.id}/favorite", **_auth(token))

    assert res.status_code == 404
    assert not Favorite.objects.exists()


@_PUBLIC_HOST
def test_favorite_and_remind_reject_a_garbage_token(channel, asset_ready, db):
    """comments/pin と同じ既知の粗さ: 無効な Bearer は 401 でなく 403 になる。"""
    p = _program(channel, asset_ready)
    client = Client(enforce_csrf_checks=True)

    fav = client.post(f"/api/v1/program/{p.id}/favorite", HTTP_AUTHORIZATION="Bearer nope")
    rem = client.post(f"/api/v1/program/{p.id}/remind", HTTP_AUTHORIZATION="Bearer nope")

    assert fav.status_code == 403 and rem.status_code == 403
    assert not Favorite.objects.exists() and not Reminder.objects.exists()


@_PUBLIC_HOST
def test_favorite_toggle_also_reflected_via_session_cookie(channel, asset_ready, db):
    """member_any_auth なので session cookie (web の島) からも引き続き叩ける。"""
    _member()
    p = _program(channel, asset_ready)
    http_client = Client()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})

    res = http_client.post(f"/api/v1/program/{p.id}/favorite")

    assert res.status_code == 200
    assert res.json()["favorited"] is True
