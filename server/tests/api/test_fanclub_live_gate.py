# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ライブ配信のファンクラブ ティアゲート (#27 Phase B、exposure_policy 統合)。

scheduling.exposure_gate の can_watch_live/gate_reason/hls_url_for が fc_required_level を
サイト会員軸(既存)と併せて判定すること、および「共有 Live Input がゲートされていない」問題
(/api/v1/home のホームプレビュー・/live/<slug>/poster.jpg のライブ静止画が /api/v1/channels/{slug}
と同じゲートを素通りしていた) が解消されたことを検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorSeriesLink, CreatorTier
from members.models import Member
from scheduling.exposure_gate import can_watch_live, gate_reason, hls_url_for
from scheduling.models import Program, ProgramType, Series

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


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


def _live_program(channel, asset, *, series=None, fc_required_level=None, exposure_policy=""):
    """「現在番組」を作る。type=recorded+asset (chk_program_source 制約の単純化。現在番組の
    解決 (channel_detail/card) は start_at<=now<end_at のみを見るため type=live 限定ではない、
    tests/test_api_player_exposure_gate.py と同じ方式)。
    """
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        title="現在番組",
        type=ProgramType.RECORDED,
        asset=asset,
        series=series,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
        fc_required_level=fc_required_level,
        exposure_policy=exposure_policy,
    )


# ---- scheduling.exposure_gate 単体 (fc_required_level 軸) ----


def test_can_watch_live_fanclub_requires_membership(db, channel, asset_ready):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()

    assert can_watch_live(p, None) is False
    assert can_watch_live(p, m) is False
    fc_services.join(m, c, tier)
    assert can_watch_live(p, m) is True


def test_gate_reason_fanclub_branches(db, channel, asset_ready):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()

    assert gate_reason(p, None) == "login"
    assert gate_reason(p, m) == "fc_join"
    fc_services.join(m, c, tier)
    assert gate_reason(p, m) == ""


def test_can_watch_live_none_program_is_public(db):
    assert can_watch_live(None, None) is True
    assert gate_reason(None, None) == ""


def test_can_watch_live_unlinked_series_fails_closed(db, channel, asset_ready):
    s = _series(channel, creator=None)
    p = _live_program(channel, asset_ready, series=s, fc_required_level=0)
    assert can_watch_live(p, None) is False
    assert gate_reason(p, None) == "fc_unavailable"


def test_can_watch_live_requires_both_site_and_fc_axis(db, channel, asset_ready):
    """site_members(サイト会員軸) と fc_required_level(ファンクラブ軸) は両方満たす必要がある。"""
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    p = _live_program(
        channel, asset_ready, series=s, fc_required_level=0, exposure_policy="site_members"
    )
    m = _member()
    fc_services.join(m, c, tier)  # ファンクラブ軸は満たすがサイト会員(サブスク)ではない

    assert can_watch_live(p, m) is False
    assert gate_reason(p, m) == "subscribe"


def test_hls_url_for_withholds_url_when_fc_gated(db, channel, asset_ready):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    s = _series(channel, creator=c)
    p = _live_program(channel, asset_ready, series=s, fc_required_level=1)

    assert hls_url_for(channel, p, None) is None


# ---- API: /api/v1/channels/{slug} (プレイヤー本体) ----


def test_channel_detail_hides_hls_url_from_anonymous_when_fc_gated(channel, asset_ready, db):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["gate_reason"] == "login"


@_PUBLIC_HOST
def test_channel_detail_exposes_url_to_correct_tier_member(channel, asset_ready, http_client, db):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)

    d = http_client.get("/api/v1/channels/ch1").json()
    # ファンクラブ ティア軸もゲート対象のため署名トークン付き (サイト会員軸と同じ扱いに統一)。
    assert d["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert d["gate_reason"] == ""


# ---- アドバーサリアル レビューで発見した穴の回帰確認 ----


def test_channel_detail_and_home_are_not_shared_cacheable(channel, asset_ready, db):
    """署名付き再生URL/ゲート理由は視聴者ごとに異なるため、CDN/共有キャッシュに載せない

    (レビュー指摘: live_poster_serve には元々あった対策がこの2エンドポイントには無く、
    ある視聴者の signed hls_url がキャッシュされ別の視聴者へそのまま配信され得た)。
    """
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])

    r1 = Client().get("/api/v1/channels/ch1")
    assert r1["Cache-Control"] == "private, no-store"
    r2 = Client().get("/api/v1/home")
    assert r2["Cache-Control"] == "private, no-store"


def test_channel_detail_hides_youtube_broadcast_id_when_gated(channel, asset_ready, db):
    """YouTube rolling枠 iframe フォールバックも共有Live Inputを映すため、ゲート中は

    hls_url と同様に youtube_broadcast_id も渡さない (レビュー指摘: 渡すと VideoPlayer.tsx が
    hlsUrl→youtubeBroadcastId のフォールバック順で iframe を先に描画してしまい、
    site-member/ファンクラブ ティア軸のゲートがまるごと素通りしていた)。
    """
    from youtube.models import YoutubeSlot, YtSlotStatus

    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)
    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now - timedelta(minutes=5),
        window_end=now + timedelta(minutes=5),
        status=YtSlotStatus.LIVE,
        broadcast_id="ytbroadcast123",
    )

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["youtube_broadcast_id"] is None


@_PUBLIC_HOST
def test_channel_detail_exposes_youtube_broadcast_id_when_entitled(
    channel, asset_ready, http_client, db
):
    from youtube.models import YoutubeSlot, YtSlotStatus

    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now - timedelta(minutes=5),
        window_end=now + timedelta(minutes=5),
        status=YtSlotStatus.LIVE,
        broadcast_id="ytbroadcast123",
    )

    d = http_client.get("/api/v1/channels/ch1").json()
    assert d["youtube_broadcast_id"] == "ytbroadcast123"


def test_channel_detail_current_program_exposes_series_url_for_join_cta(channel, asset_ready, db):
    """fc_join 遷移先の安全な導線 (レビュー指摘: /program/<id>/ は public_visible のみで

    ゲート対象のあらすじ/出演者/サムネを晒すため、series_url (ファンクラブ カードが正しく
    ゲートされる) を使う。current.series_url がその素材になる)。
    """
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["current"]["series_url"] == s.public_url


# ---- API: /api/v1/home (ホーム ヒーロー/カード プレビュー) ----
# 過去、channel_detail のみゲートが実装され home 側は素通りしていた (「共有 Live Input が
# ゲートされていない問題」)。ここで両エンドポイントの一致を明示的に検証する。


def test_home_hides_hls_url_from_anonymous_when_fc_gated(channel, asset_ready, db):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    d = Client().get("/api/v1/home").json()
    card = d["cards"][0]
    assert card["hls_url"] == ""
    assert card["gate_reason"] == "login"


@_PUBLIC_HOST
def test_home_exposes_url_to_correct_tier_member(channel, asset_ready, http_client, db):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)

    d = http_client.get("/api/v1/home").json()
    card = d["cards"][0]
    assert card["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert card["gate_reason"] == ""


def test_home_still_exposes_url_for_public_program(channel, asset_ready, db):
    """回帰防止: ゲート追加が既存の完全公開番組の露出を壊していないこと。"""
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, asset_ready)  # series/fc_required_level 無し = 完全公開

    d = Client().get("/api/v1/home").json()
    card = d["cards"][0]
    assert card["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert card["gate_reason"] == ""


# ---- ライブ静止画 (/live/<slug>/poster.jpg) ----


@_PUBLIC_HOST
def test_live_poster_404_when_fc_gated(channel, asset_ready, db, monkeypatch):
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    resp = Client().get("/live/ch1/poster.jpg")
    assert resp.status_code == 404


@_PUBLIC_HOST
def test_live_poster_served_with_private_cache_for_correct_tier_member(
    channel, asset_ready, http_client, db, monkeypatch
):
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)

    resp = http_client.get("/live/ch1/poster.jpg")
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "private, no-store"


@_PUBLIC_HOST
def test_live_poster_keeps_public_cache_for_ungated_program(channel, asset_ready, db, monkeypatch):
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    _live_program(channel, asset_ready)  # 完全公開

    resp = Client().get("/live/ch1/poster.jpg")
    assert resp.status_code == 200
    assert f"max-age={live_poster.SERVE_TTL}" in resp["Cache-Control"]


@_PUBLIC_HOST
def test_live_poster_gated_404_is_not_shared_cached(channel, asset_ready, db, monkeypatch):
    """ゲート対象の 404 は per-viewer の判定結果なので共有キャッシュに残さない。

    .jpg は CDN 既定でキャッシュ対象になりやすく、無指定だとゲート解除後もこの 404 が
    居座って全視聴者へ配られる (キャッシュ境界の逆向きの漏れ)。
    """
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    resp = Client().get("/live/ch1/poster.jpg")
    assert resp.status_code == 404
    assert resp["Cache-Control"] == "private, no-store"


@_PUBLIC_HOST
def test_live_poster_clamps_public_cache_at_program_boundary(channel, asset_ready, db, monkeypatch):
    """公開番組の終了直前は、境界を跨いでキャッシュが残らないよう max-age を残り秒数へ切る。"""
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        title="もうすぐ終わる公開番組",
        type=ProgramType.RECORDED,
        asset=asset_ready,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(seconds=3),
        public_visible=True,
    )

    resp = Client().get("/live/ch1/poster.jpg")
    assert resp.status_code == 200
    max_age = int(resp["Cache-Control"].split("max-age=")[1])
    assert 0 <= max_age <= 3


@_PUBLIC_HOST
def test_live_poster_clamps_public_cache_before_next_program(channel, asset_ready, db, monkeypatch):
    """現在番組が無いときは、次番組の開始までの残り秒数でクランプする。"""
    from core import live_poster

    monkeypatch.setattr(live_poster, "get", lambda slug: b"\xff\xd8jpeg")
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        title="まもなく始まるFC限定番組",
        type=ProgramType.RECORDED,
        asset=asset_ready,
        start_at=now + timedelta(seconds=4),
        end_at=now + timedelta(minutes=30),
        public_visible=True,
        fc_required_level=1,
    )

    resp = Client().get("/live/ch1/poster.jpg")
    assert resp.status_code == 200
    max_age = int(resp["Cache-Control"].split("max-age=")[1])
    assert 0 <= max_age <= 4


# ---- fanclub/tasks.py: creator YouTube 宛先シミュルキャストの防御的スキップ ----


def test_simulcast_program_skips_fc_gated_program(db, channel, asset_ready):
    from fanclub.tasks import _simulcast_program

    c = _creator()
    s = _series(channel, creator=c)
    _live_program(channel, asset_ready, series=s, fc_required_level=0)

    assert _simulcast_program(c, timezone.now(), current_channel_id=None) is None


def test_simulcast_program_returns_public_program(db, channel, asset_ready):
    from fanclub.tasks import _simulcast_program

    c = _creator()
    s = _series(channel, creator=c)
    p = _live_program(channel, asset_ready, series=s)  # fc_required_level=None = 完全公開

    assert _simulcast_program(c, timezone.now(), current_channel_id=None) == p


def test_creator_tier_level_gate_still_works_for_paid_tier(db, channel, asset_ready):
    """有料ティア(level>0)も無料ティアと同じ判定ロジックで正しく解決されること。"""
    c = _creator()
    fc_services.ensure_free_tier(c)
    paid = CreatorTier.objects.create(
        creator=c, level=1, name="ベーシック", price_minor=500, stripe_price_id="price_x"
    )
    c.stripe_connect_account_id = "acct_test"
    c.stripe_connect_onboarded = True
    c.save(update_fields=["stripe_connect_account_id", "stripe_connect_onboarded"])
    s = _series(channel, creator=c)
    p = _live_program(channel, asset_ready, series=s, fc_required_level=1)
    m = _member()

    assert can_watch_live(p, m) is False
    assert gate_reason(p, m) == "fc_join"
    fc_services.join_free_tier(m, c)
    assert can_watch_live(p, m) is False  # 無料ティアのみでは有料ティア限定を満たさない

    from fanclub.models import CreatorMembership, MembershipStatus

    CreatorMembership.objects.filter(member=m, creator=c).update(
        tier=paid, status=MembershipStatus.ACTIVE
    )
    assert can_watch_live(p, m) is True
