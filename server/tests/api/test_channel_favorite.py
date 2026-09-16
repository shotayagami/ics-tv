# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""お気に入りチャンネル (ピン留め) #EPG-04: トグル / 並べ替え / UI。"""

from __future__ import annotations

from django.test import override_settings
from django.utils import timezone

from core.models import Channel
from members.models import ChannelFavorite, Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(client, email="m@example.com"):
    client.post("/members/login/", {"email": email, "password": _PW})


def _ch2():
    return Channel.objects.create(
        name="ICS-TV 教育", slug="ch2", enabled=True, agent_token="tok-ch2-xyz"
    )


@_PUBLIC_HOST
def test_pin_toggle_requires_login(http_client, channel, db):
    res = http_client.post("/members/channels/ch1/toggle/")
    assert res.status_code == 302 and "/members/login/" in res.url


@_PUBLIC_HOST
def test_pin_toggle_creates_and_removes(http_client, channel, db):
    m = _member()
    _login(http_client)
    r1 = http_client.post("/members/channels/ch1/toggle/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r1.json()["pinned"] is True
    assert ChannelFavorite.objects.filter(member=m, channel=channel).exists()
    r2 = http_client.post("/members/channels/ch1/toggle/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r2.json()["pinned"] is False
    assert not ChannelFavorite.objects.filter(member=m, channel=channel).exists()


@_PUBLIC_HOST
def test_channel_detail_is_pinned_for_member(http_client, channel, db):
    # #Phase1: pin ボタン/状態は島が API の is_pinned から描画する。
    m = _member()
    _login(http_client)
    assert http_client.get("/api/v1/channels/ch1").json()["is_pinned"] is False
    ChannelFavorite.objects.create(member=m, channel=channel)
    assert http_client.get("/api/v1/channels/ch1").json()["is_pinned"] is True


@_PUBLIC_HOST
def test_channel_detail_anon_not_pinned(http_client, channel, db):
    # 未ログインは is_pinned False (島の pin 操作は 401 → ログインへ誘導)。
    assert http_client.get("/api/v1/channels/ch1").json()["is_pinned"] is False


@_PUBLIC_HOST
def test_channel_tabs_mark_pinned(http_client, channel, db):
    # #Phase1: タブの ★ は島が API channels の pinned フラグから描画する。
    m = _member()
    _login(http_client)
    ChannelFavorite.objects.create(member=m, channel=channel)
    tabs = http_client.get("/api/v1/channels").json()
    assert next(c for c in tabs if c["slug"] == "ch1")["pinned"] is True


@_PUBLIC_HOST
def test_home_orders_pinned_channel_first(http_client, channel, db):
    ch2 = _ch2()
    m = _member()
    _login(http_client)
    ChannelFavorite.objects.create(member=m, channel=ch2)  # ch2 をピン
    # #Phase2b: カード順は島が /api/v1/home (member-aware) から取得。ピン ch が先頭。
    slugs = [c["slug"] for c in http_client.get("/api/v1/home").json()["cards"]]
    assert slugs.index("ch2") < slugs.index("ch1")
