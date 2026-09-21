# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴年齢制限 (#BILL-02): レーティング解決 / 満年齢 / 年齢ゲート / VOD 視聴ブロック・バッジ。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from members.models import Member
from scheduling.models import ContentRating, Program, ProgramType, Series, VodVisibility
from scheduling.ratings import age_gate_reason

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_SIGNED = "https://r2.example/signed.mp4?sig=x"


def _member(email="m@example.com", *, birth_year=1990, birth_month=4, verified=True):
    m = Member(
        email=email,
        nickname="みんと",
        birth_year=birth_year,
        birth_month=birth_month,
        postal_code="1000001",
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _program(channel, asset, *, rating="", series=None, visibility=VodVisibility.PUBLIC):
    end = timezone.now() - timedelta(hours=2)
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="放送済み番組",
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        rating=rating,
        series=series,
        vod_visibility=visibility,
    )


def _aware(y, mo, d):
    return timezone.make_aware(datetime(y, mo, d, 12, 0))


# ---- モデル: レーティング解決 / min_age ----


def test_resolved_rating_falls_back_to_series(channel, db):
    s = Series.objects.create(channel=channel, title="S", rating=ContentRating.R18)
    p = Program(series=s, rating="")
    assert p.resolved_rating == "r18"
    assert p.resolved_rating_label == "R18+"
    assert p.min_age == 18


def test_program_rating_overrides_series(channel, db):
    s = Series.objects.create(channel=channel, title="S", rating=ContentRating.R18)
    p = Program(series=s, rating=ContentRating.R15)
    assert p.resolved_rating == "r15" and p.min_age == 15


def test_no_rating_is_unrestricted(db):
    p = Program(rating="")
    assert p.resolved_rating == "" and p.resolved_rating_label == "" and p.min_age == 0


def test_pg12_is_advisory_not_hard_gate(db):
    p = Program(rating=ContentRating.PG12)
    assert p.resolved_rating_label == "PG12" and p.min_age == 0


# ---- 会員: 満年齢 (月精度) ----


def test_member_age_month_precision(db):
    m = _member(birth_year=2008, birth_month=6)
    assert m.age(now=_aware(2026, 6, 1)) == 18  # 誕生月到来で歳を取る
    assert m.age(now=_aware(2026, 5, 31)) == 17  # 誕生月前は未満
    assert m.age(now=_aware(2026, 12, 1)) == 18


# ---- ゲート関数 ----


def test_age_gate_reason_paths(db):
    p18 = Program(rating=ContentRating.R18)
    assert age_gate_reason(Program(rating=""), None) == ""  # 制限なしは未ログインでも可
    assert age_gate_reason(p18, None) == "login"  # 年齢不明
    young = _member(birth_year=2015, birth_month=1)
    assert age_gate_reason(p18, young) == "age"  # 年齢不足
    adult = _member(email="a@example.com", birth_year=1990, birth_month=1)
    assert age_gate_reason(p18, adult) == ""  # 充足


# ---- VOD 視聴ゲート (年齢は可視性より優先) ----


@_PUBLIC_HOST
def test_vod_r18_anon_redirects_to_login(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_vod_r18_underage_member_blocked_403(http_client, channel, asset_ready, db):
    _member(birth_year=2015, birth_month=1)
    _login(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 403
    body = res.content.decode("utf-8")
    assert "年齢制限により視聴できません" in body and "R18+" in body
    assert _SIGNED not in body  # 署名URLは渡さない


@_PUBLIC_HOST
def test_vod_r18_adult_member_plays(http_client, channel, asset_ready, db):
    _member(birth_year=1990, birth_month=1)
    _login(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 200 and _SIGNED in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_vod_pg12_plays_for_anon(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, rating=ContentRating.PG12)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 200  # PG12 は助言のみ=ハードゲート無し


@_PUBLIC_HOST
def test_vod_age_takes_priority_over_visibility(http_client, channel, asset_ready, db):
    # 会員限定 (verified なら可視性は通る) でも R18 で年齢不足なら age で塞ぐ。
    _member(birth_year=2015, birth_month=1, verified=True)
    _login(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18, visibility=VodVisibility.MEMBERS)
    res = http_client.get(f"/vod/{p.id}/")
    assert res.status_code == 403  # verify ではなく age が優先


@_PUBLIC_HOST
def test_vod_detail_shows_rating_badge(http_client, channel, asset_ready, db):
    _member(birth_year=1990, birth_month=1)
    _login(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        res = http_client.get(f"/vod/{p.id}/")
    assert "R18+" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_program_detail_shows_rating_badge(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, rating=ContentRating.R15)
    res = http_client.get(f"/program/{p.id}/")
    assert res.status_code == 200 and "R15+" in res.content.decode("utf-8")
