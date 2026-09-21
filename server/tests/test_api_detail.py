# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""番組詳細 操作バー API (#Phase2c): /api/v1/program/{id}。

状態別CTA (live/aired-vod/aired-none/upcoming) と 会員操作の初期状態 (お気に入り/リマインド/
認証可否) を検証する。本文 (タイトル/あらすじ/出演者) は SSR のため test_program_detail.py 側。
トグルの POST 自体は既存 member エンドポイント (test_members_*) の責務。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Favorite, Member, Reminder
from scheduling.models import Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _program(channel, asset, *, start_delta, dur=timedelta(hours=1), visibility=VodVisibility.OFF):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組X",
        start_at=start,
        end_at=start + dur,
        asset=asset,
        public_visible=True,
        vod_visibility=visibility,
    )


def _member(verified=True):
    m = Member(
        email="m@example.com",
        nickname="みんと",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


@_PUBLIC_HOST
def test_live_cta(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(minutes=-10))
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["state"] == "live"
    assert d["cta"] == {
        "kind": "live",
        "url": f"/ch/{channel.slug}/",
        "label": "▶ ライブで見る",
        "external": False,
    }


@_PUBLIC_HOST
def test_aired_vod_cta(http_client, channel, asset_ready, db):
    p = _program(
        channel, asset_ready, start_delta=timedelta(hours=-3), visibility=VodVisibility.PUBLIC
    )
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["state"] == "aired"
    assert d["cta"]["kind"] == "vod" and d["cta"]["url"] == f"/vod/{p.id}/"


@_PUBLIC_HOST
def test_aired_no_vod_has_no_cta(http_client, channel, asset_ready, db):
    p = _program(
        channel, asset_ready, start_delta=timedelta(hours=-3), visibility=VodVisibility.OFF
    )
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["cta"]["kind"] == "none"  # 録画・VOD OFF・非ライブ → 再生導線なし


@_PUBLIC_HOST
def test_upcoming_pill_no_cta(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(days=1))
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["state"] == "upcoming"
    assert d["cta"]["kind"] == "none"
    assert "放送予定" in d["start_pill"]  # planpill 文言


@_PUBLIC_HOST
def test_non_public_404(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(hours=3))
    p.public_visible = False
    p.save(update_fields=["public_visible"])
    assert http_client.get(f"/api/v1/program/{p.id}").status_code == 404


@_PUBLIC_HOST
def test_anonymous_share_meta(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(hours=3))
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["is_member"] is False
    assert d["is_favorited"] is False and d["is_reminded"] is False
    assert d["share_url"].endswith(f"/program/{p.id}/")


@_PUBLIC_HOST
def test_member_favorite_reminder_flags(http_client, channel, asset_ready, db):
    m = _member(verified=True)
    p = _program(channel, asset_ready, start_delta=timedelta(days=1))
    Favorite.objects.create(member=m, program=p)
    Reminder.objects.create(member=m, program=p)
    _login(http_client)
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["is_member"] is True
    assert d["is_favorited"] is True and d["is_reminded"] is True
    assert d["can_remind"] is True  # verified


@_PUBLIC_HOST
def test_unverified_member_cannot_remind(http_client, channel, asset_ready, db):
    _member(verified=False)
    p = _program(channel, asset_ready, start_delta=timedelta(days=1))
    _login(http_client)
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["is_member"] is True
    assert d["can_remind"] is False  # 未認証はリマインド不可 → 島は認証導線へ
