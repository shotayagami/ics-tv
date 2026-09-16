# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""見逃し再生の署名 URL API (#MOBILE-01)。

SSR 版 (core.views.public_vod_detail) と同じゲートを通ること、そして **再生不可のときに
署名 URL が本文へ漏れないこと** を確認する。ここが素通りになると年齢制限 (#BILL-02) と
可視性 (会員限定/サブスク/ファンクラブ) がまとめて抜ける。
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from members.models import Member
from scheduling.models import ContentRating, Program, ProgramType, VodVisibility
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_SIGNED = "https://r2.example/signed.mp4?sig=x"


def _member(email="m@example.com", verified=True, **over):
    fields = {"birth_year": 1990, "birth_month": 4, "postal_code": "1000001", **over}
    m = Member(email=email, nickname="みんと", **fields)
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _token(http_client, email="m@example.com"):
    res = http_client.post(
        "/api/v1/auth/login",
        {"email": email, "password": _PW},
        content_type="application/json",
    )
    return res.json()["token"]


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _program(channel, asset, *, visibility=VodVisibility.PUBLIC, rating="", ended_ago=None):
    end = timezone.now() - (ended_ago or timedelta(hours=2))
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="放送済み番組",
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=visibility,
        rating=rating,
    )


def _play(http_client, program, **extra):
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        return http_client.get(f"/api/v1/vod/{program.pk}/play", **extra)


# ---- 公開 VOD ----


@_PUBLIC_HOST
def test_public_vod_plays_anonymously(channel, asset_ready, db, http_client):
    p = _program(channel, asset_ready)
    res = _play(http_client, p)
    assert res.status_code == 200
    body = res.json()
    assert body["can_watch"] is True
    assert body["gate"] == ""
    assert body["url"] == _SIGNED
    assert body["expires_in"] > 0
    assert body["duration_ms"] == asset_ready.duration_ms


@_PUBLIC_HOST
def test_expiry_follows_duration_clamp(channel, asset_ready, db, http_client):
    """尺の2倍を 1h〜6h でクランプ (vod.playback_url_with_expiry と同じ規則)。"""
    p = _program(channel, asset_ready)  # 1h 素材 → 2h
    assert _play(http_client, p).json()["expires_in"] == 7200


# ---- 可視性ゲート ----


@_PUBLIC_HOST
def test_members_only_vod_is_403_for_anonymous(channel, asset_ready, db, http_client):
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    res = _play(http_client, p)
    assert res.status_code == 403
    body = res.json()
    assert body["can_watch"] is False
    assert body["gate"] == "login"
    assert body["url"] == ""  # 署名 URL を漏らさない


@_PUBLIC_HOST
def test_members_only_vod_plays_with_token(channel, asset_ready, db, http_client):
    _member()
    token = _token(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 200
    assert res.json()["url"] == _SIGNED


@_PUBLIC_HOST
def test_unverified_member_is_gated_to_verify(channel, asset_ready, db, http_client):
    _member(verified=False)
    token = _token(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.MEMBERS)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 403
    assert res.json()["gate"] == "verify"
    assert res.json()["url"] == ""


@_PUBLIC_HOST
def test_subscriber_only_vod_gates_plain_member(channel, asset_ready, db, http_client):
    _member()
    token = _token(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 403
    assert res.json()["gate"] == "subscribe"
    assert res.json()["url"] == ""


@_PUBLIC_HOST
def test_subscriber_only_vod_plays_for_subscriber(channel, asset_ready, db, http_client):
    m = _member()
    plan = Plan.objects.create(name="松", slug="matsu", amount=980, rank=3)
    MemberSubscription.objects.create(
        member=m,
        plan=plan,
        status=SubStatus.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=30),
    )
    token = _token(http_client)
    p = _program(channel, asset_ready, visibility=VodVisibility.SUBSCRIBERS)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 200
    assert res.json()["url"] == _SIGNED


# ---- 年齢制限 (#BILL-02) ----


@_PUBLIC_HOST
def test_r18_is_blocked_for_anonymous(channel, asset_ready, db, http_client):
    """匿名は年齢不明なので "age" ではなく "login" (ログインさせて年齢を確かめる導線)。"""
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    res = _play(http_client, p)
    assert res.status_code == 403
    assert res.json()["gate"] == "login"
    assert res.json()["url"] == ""


@_PUBLIC_HOST
def test_r18_is_blocked_for_minor(channel, asset_ready, db, http_client):
    minor_birth_year = timezone.now().year - 15
    _member(birth_year=minor_birth_year)
    token = _token(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 403
    assert res.json()["gate"] == "age"
    assert res.json()["url"] == ""


@_PUBLIC_HOST
def test_r18_plays_for_adult_member(channel, asset_ready, db, http_client):
    _member()  # birth_year=1990 → 成人
    token = _token(http_client)
    p = _program(channel, asset_ready, rating=ContentRating.R18)
    res = _play(http_client, p, **_auth(token))
    assert res.status_code == 200
    assert res.json()["url"] == _SIGNED


# ---- そもそも VOD として存在しない ----


@_PUBLIC_HOST
def test_unknown_program_is_404(db, http_client):
    assert _play(http_client, mock.Mock(pk=999999)).status_code == 404


@_PUBLIC_HOST
def test_vod_off_program_is_404(channel, asset_ready, db, http_client):
    """VOD 非公開は「見えない」であって「ゲートされている」ではない。"""
    p = _program(channel, asset_ready, visibility=VodVisibility.OFF)
    assert _play(http_client, p).status_code == 404


@_PUBLIC_HOST
def test_not_yet_broadcast_program_is_404(channel, asset_ready, db, http_client):
    p = _program(channel, asset_ready, ended_ago=timedelta(hours=-2))  # 終了が未来
    assert _play(http_client, p).status_code == 404


@_PUBLIC_HOST
def test_expired_availability_window_is_404(channel, asset_ready, db, http_client):
    p = _program(channel, asset_ready)
    Program.objects.filter(pk=p.pk).update(vod_available_until=timezone.now() - timedelta(days=1))
    assert _play(http_client, p).status_code == 404


# ---- セキュリティレビュー由来の回帰テスト ----


@_PUBLIC_HOST
def test_response_is_not_shared_cacheable(channel, asset_ready, db, http_client):
    """本文に署名 URL と会員ごとの判定が乗るため、共有キャッシュを禁じること。

    SSR 版 (public_vod_detail) は no-store を付けている。API 側で抜けると Cloudflare が
    ある会員向けの署名 URL を別の閲覧者へ配りうる。
    """
    p = _program(channel, asset_ready)
    assert _play(http_client, p)["Cache-Control"] == "no-store"

    # 同一チャンネルで時間帯が重なると排他制約に当たるため、別の時間帯に置く。
    gated = _program(
        channel,
        asset_ready,
        visibility=VodVisibility.MEMBERS,
        ended_ago=timedelta(hours=6),
    )
    assert _play(http_client, gated)["Cache-Control"] == "no-store"
