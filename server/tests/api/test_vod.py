# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""見逃し配信 (VOD) サービス: 利用可否クエリ・可視性ゲート・再生URL (#VOD-01)。"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from medialib.models import Asset, AssetKind, NormalizeStatus
from members.models import Member
from scheduling import vod
from scheduling.models import Program, ProgramType, VodVisibility
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", verified=True):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _subscribe(member, status=SubStatus.ACTIVE):
    plan = Plan.objects.create(name="松", slug="matsu", amount=980, rank=3)
    return MemberSubscription.objects.create(
        member=member,
        plan=plan,
        status=status,
        current_period_end=timezone.now() + timedelta(days=30),
    )


def _program(
    channel,
    asset,
    *,
    visibility=VodVisibility.PUBLIC,
    ended_ago=timedelta(hours=2),
    until=None,
    title="放送済み番組",
):
    now = timezone.now()
    end = now - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=visibility,
        vod_available_until=until,
    )


# ---- available_vod_qs ----


def test_qs_includes_aired_public(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    assert list(vod.available_vod_qs()) == [p]


def test_qs_excludes_off(channel, asset_ready, db):
    _program(channel, asset_ready, visibility=VodVisibility.OFF)
    assert list(vod.available_vod_qs()) == []


def test_qs_excludes_not_yet_aired(channel, asset_ready, db):
    # end_at が未来 = まだ放送中/前 → 見逃し対象外
    _program(channel, asset_ready, ended_ago=timedelta(hours=-1))
    assert list(vod.available_vod_qs()) == []


def test_qs_excludes_expired_window(channel, asset_ready, db):
    _program(channel, asset_ready, until=timezone.now() - timedelta(minutes=1))
    assert list(vod.available_vod_qs()) == []


def test_qs_includes_open_window(channel, asset_ready, db):
    p = _program(channel, asset_ready, until=timezone.now() + timedelta(days=7))
    assert list(vod.available_vod_qs()) == [p]


def test_qs_excludes_unready_asset(channel, db):
    pending = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="未正規化",
        duration_ms=1000,
        r2_key="mezzanine/program/x.mp4",
        normalize_status=NormalizeStatus.PENDING,
    )
    _program(channel, pending)
    assert list(vod.available_vod_qs()) == []


# ---- can_watch (可視性ゲート) ----


def test_can_watch_public(channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.PUBLIC)
    assert vod.can_watch(p, None) is True


def test_can_watch_members(channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    assert vod.can_watch(p, None) is False
    assert vod.can_watch(p, _member(verified=False)) is False
    assert vod.can_watch(p, _member("v@example.com", verified=True)) is True


def test_can_watch_subscribers(channel, asset_ready, db):
    p = _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS)
    plain = _member("plain@example.com")
    subbed = _member("sub@example.com")
    _subscribe(subbed)
    assert vod.can_watch(p, None) is False
    assert vod.can_watch(p, plain) is False
    assert vod.can_watch(p, subbed) is True


def test_gate_reason(channel, asset_ready, db):
    members = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    # 別番組: 時間レンジを十分離して排他制約 (per-channel 重複禁止) を避ける
    subs = _program(
        channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS, ended_ago=timedelta(hours=5)
    )
    assert vod.gate_reason(members, None) == "login"
    assert vod.gate_reason(members, _member(verified=False)) == "verify"
    assert vod.gate_reason(subs, None) == "login"
    assert vod.gate_reason(subs, _member("p@example.com")) == "subscribe"
    assert vod.gate_reason(members, _member("v@example.com")) == ""  # 視聴可なら理由なし


# ---- playback_url ----


def test_playback_url_presigns_with_clamped_expiry(channel, asset_ready, db):
    p = _program(channel, asset_ready)  # duration 3_600_000ms → *2=7200s (clamp内)
    with mock.patch("core.r2.presign_get", return_value="https://signed/url") as pg:
        url = vod.playback_url(p)
    assert url == "https://signed/url"
    pg.assert_called_once_with(asset_ready.r2_key, expires=7200)
