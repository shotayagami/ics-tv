# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""見逃し配信 公開UI: 一覧表示・可視性ゲートのリダイレクト・署名URL再生 (#VOD-01)。"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from members.models import Member
from scheduling.models import Program, ProgramType, VodVisibility
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_SIGNED = "https://r2.example/signed.mp4?sig=x"


def _member(email="m@example.com", verified=True):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _subscribe(member):
    plan = Plan.objects.create(name="松", slug="matsu", amount=980, rank=3)
    MemberSubscription.objects.create(
        member=member,
        plan=plan,
        status=SubStatus.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=30),
    )


def _program(
    channel,
    asset,
    *,
    visibility=VodVisibility.PUBLIC,
    ended_ago=timedelta(hours=2),
    title="放送済み番組",
):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=visibility,
    )


# ---- 一覧 ----


# #Phase2c: 一覧は React 島が /api/v1/vod から描画 → API ベースで検証。詳細/再生ゲートは SSR のまま。


@_PUBLIC_HOST
def test_list_shows_available_hides_off(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="見られる番組")
    titles = [c["title"] for c in http_client.get("/api/v1/vod").json()["items"]]
    assert "見られる番組" in titles


@_PUBLIC_HOST
def test_list_excludes_off(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, visibility=VodVisibility.OFF, title="非公開番組")
    d = http_client.get("/api/v1/vod").json()
    assert all(c["title"] != "非公開番組" for c in d["items"])
    assert d["items"] == []  # OFF は available_vod_qs に出ない


@_PUBLIC_HOST
def test_list_shows_visibility_badges(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS, title="限定番組")
    items = http_client.get("/api/v1/vod").json()["items"]
    assert any(c["vod_visibility"] == "subscribers" for c in items)


# ---- 詳細/再生ゲート ----


@_PUBLIC_HOST
def test_detail_public_plays_with_signed_url(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.PUBLIC)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert _SIGNED in body and "放送済み番組" in body
    assert res["Cache-Control"] == "no-store"  # 署名URL入りはキャッシュ禁止


@_PUBLIC_HOST
def test_detail_off_is_404(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.OFF)
    assert http_client.get(f"/vod/{p.id}/").status_code == 404


@_PUBLIC_HOST
def test_detail_members_anon_redirects_to_login(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")
    assert "next" in res.url


@_PUBLIC_HOST
def test_detail_members_unverified_redirects_to_verify(http_client, channel, asset_ready, db):
    _member(verified=False)
    _login(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url == "/members/verify-required/"


@_PUBLIC_HOST
def test_detail_subscribers_plain_member_redirects_to_subscribe(
    http_client, channel, asset_ready, db
):
    _member()
    _login(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url == "/subscriptions/"


@_PUBLIC_HOST
def test_detail_subscriber_plays(http_client, channel, asset_ready, db):
    m = _member()
    _subscribe(m)
    _login(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 200 and _SIGNED in res.content.decode("utf-8")
