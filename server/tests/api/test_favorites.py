# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""マイリスト (#PERS-01): お気に入りトグル・一覧・番組詳細のボタン状態。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Favorite, Member
from scheduling.models import Program, ProgramType

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _program(channel, asset, *, title="番組X", public=True, start_delta=timedelta(hours=3)):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        public_visible=public,
    )


@_PUBLIC_HOST
def test_toggle_adds_then_removes(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    r1 = http_client.post(f"/members/favorites/{p.id}/toggle/", **_AJAX)
    assert r1.status_code == 200 and r1.json()["favorited"] is True
    assert Favorite.objects.filter(member=m, program=p).exists()
    r2 = http_client.post(f"/members/favorites/{p.id}/toggle/", **_AJAX)
    assert r2.json()["favorited"] is False
    assert not Favorite.objects.filter(member=m, program=p).exists()


@_PUBLIC_HOST
def test_toggle_requires_login(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready)
    res = http_client.post(f"/members/favorites/{p.id}/toggle/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")
    assert not Favorite.objects.exists()


@_PUBLIC_HOST
def test_list_shows_favorites(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready, title="お気に入り番組")
    Favorite.objects.create(member=m, program=p)
    body = http_client.get("/members/favorites/").content.decode("utf-8")
    assert "お気に入り番組" in body


@_PUBLIC_HOST
def test_detail_button_reflects_state(http_client, channel, asset_ready, db):
    # #Phase2c: お気に入りボタンは操作バー島が /api/v1/program/{id} の is_favorited から描画。
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    assert http_client.get(f"/api/v1/program/{p.id}").json()["is_favorited"] is False  # 未登録
    Favorite.objects.create(member=m, program=p)
    assert http_client.get(f"/api/v1/program/{p.id}").json()["is_favorited"] is True  # 登録済


@_PUBLIC_HOST
def test_toggle_non_public_404(http_client, channel, asset_ready, db):
    _member()
    _login(http_client)
    p = _program(channel, asset_ready, public=False)
    assert http_client.post(f"/members/favorites/{p.id}/toggle/", **_AJAX).status_code == 404
