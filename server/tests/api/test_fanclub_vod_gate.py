# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VOD のファンクラブ限定ゲート (#27)。scheduling.vod の can_watch/gate_reason 拡張 + SSR 導線。"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorSeriesLink
from members.models import Member
from scheduling import vod as vod_mod
from scheduling.models import Program, ProgramType, Series, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_SIGNED = "https://r2.example/signed.mp4?sig=x"


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a"):
    return Creator.objects.create(name="サークルA", slug=slug)


def _series(channel, creator=None, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    if creator is not None:
        CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


def _program(channel, asset, *, series=None, fc_required_level=0, ended_ago=timedelta(hours=2)):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="FC限定番組",
        series=series,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=VodVisibility.FANCLUB,
        fc_required_level=fc_required_level,
    )


# ---- can_watch / gate_reason (関数単体) ----


def test_can_watch_fanclub_requires_membership(db, channel, asset_ready):
    c = _creator()
    services_tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()

    assert vod_mod.can_watch(p, None) is False
    assert vod_mod.can_watch(p, m) is False
    fc_services.join(m, c, services_tier)
    assert vod_mod.can_watch(p, m) is True


def test_gate_reason_fanclub_branches(db, channel, asset_ready):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()

    assert vod_mod.gate_reason(p, None) == "login"
    assert vod_mod.gate_reason(p, m) == "fc_join"
    fc_services.join(m, c, tier)
    assert vod_mod.gate_reason(p, m) == ""


def test_gate_reason_fanclub_unlinked_series_is_unavailable(db, channel, asset_ready):
    s = _series(channel, creator=None)
    p = _program(channel, asset_ready, series=s)
    assert vod_mod.gate_reason(p, None) == "fc_unavailable"


def test_gate_reason_fanclub_paid_level_is_unavailable_in_phase_a(db, channel, asset_ready):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s, fc_required_level=1)
    m = _member()
    assert vod_mod.gate_reason(p, m) == "fc_unavailable"


def test_age_gate_takes_priority_over_fanclub(db, channel, asset_ready):
    """年齢制限ゲートは可視性ゲートより優先される既存仕様の回帰確認 (fanclub混入で崩れないこと)。"""
    from scheduling.models import ContentRating

    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s, fc_required_level=0)
    p.rating = ContentRating.R18
    p.save(update_fields=["rating"])
    m = _member()
    m.birth_year = timezone.now().year - 10  # 未成年
    m.save(update_fields=["birth_year"])
    fc_services.join(m, c, tier)
    assert vod_mod.gate_reason(p, m) == "age"


# ---- SSR (public_vod_detail) ----


@_PUBLIC_HOST
def test_ssr_anonymous_redirects_to_login(http_client, channel, asset_ready, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_ssr_not_joined_redirects_to_series_page(http_client, channel, asset_ready, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s)
    _member()
    _login(http_client)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url == s.public_url


@_PUBLIC_HOST
def test_ssr_unavailable_renders_403_interstitial(http_client, channel, asset_ready, db):
    s = _series(channel, creator=None)  # creator 未紐付 = fc_unavailable
    p = _program(channel, asset_ready, series=s)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 403
    assert "ファンクラブ限定" in res.content.decode("utf-8")
    assert res["Cache-Control"] == "no-store"


@_PUBLIC_HOST
def test_ssr_joined_member_plays(http_client, channel, asset_ready, db):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 200
    assert _SIGNED in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_captions_follow_same_gate(http_client, channel, asset_ready, db):
    """字幕プロキシ (public_vod_captions) は can_watch を再利用するのみ (無改修で追随)。"""
    c = _creator()
    s = _series(channel, creator=c)
    p = _program(channel, asset_ready, series=s)
    res = http_client.get(f"/vod/{p.id}/captions.vtt")
    assert res.status_code == 404  # 未加入 (=視聴不可) なので字幕も 404
