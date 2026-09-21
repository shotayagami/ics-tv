# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exposure_policy (#27) のサイト会員限定 HLS エッジ認証を検証 (docs/site-only-broadcast.md §4.7)。

現在番組の resolved_exposure_policy に応じて /api/v1/channels/{slug} の hls_url を
出し分け、site_members/members_yt_site は未加入者に隠し gate_reason を埋める。
署名トークンの発行/検証 (/api/v1/hls-auth) も併せて検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.test import Client, override_settings
from django.utils import timezone

from members.models import Member
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", nickname="視聴者"):
    m = Member(
        email=email, nickname=nickname, birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _subscribe(member, **feats):
    plan = Plan.objects.create(name="松", slug="matsu", amount=980, rank=3, **feats)
    return MemberSubscription.objects.create(
        member=member,
        plan=plan,
        status=SubStatus.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=30),
    )


def _live_program(channel, *, exposure_policy: str, asset):
    from scheduling.models import Program

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        title="現在番組",
        type="recorded",
        asset=asset,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
        exposure_policy=exposure_policy,
    )


def test_public_policy_hls_url_open_to_anonymous(channel, asset_ready):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, exposure_policy="public", asset=asset_ready)

    d = Client().get("/api/v1/channels/ch1").json()
    # #27: エッジ (Cloudflare Worker) 強制のため完全公開でも署名トークンを付ける。
    assert d["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert d["gate_reason"] == ""


def test_site_members_policy_hides_hls_url_from_anonymous(channel, asset_ready):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, exposure_policy="site_members", asset=asset_ready)

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["gate_reason"] == "login"


@_PUBLIC_HOST
def test_site_members_policy_hides_hls_url_from_logged_in_non_subscriber(
    channel, asset_ready, http_client, db
):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, exposure_policy="site_members", asset=asset_ready)
    _member()
    _login(http_client)

    d = http_client.get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["gate_reason"] == "subscribe"


@_PUBLIC_HOST
def test_site_members_policy_exposes_signed_url_to_subscriber(
    channel, asset_ready, http_client, db
):
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, exposure_policy="site_members", asset=asset_ready)
    m = _member()
    _subscribe(m, feat_exclusive=True)
    _login(http_client)

    d = http_client.get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is not None
    assert d["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert d["gate_reason"] == ""


def test_members_yt_site_policy_gates_identically_to_site_members(
    channel, asset_ready, http_client, db
):
    """docs のポリシー表どおり、サイトHLS 軸は site_members/members_yt_site で同一。"""
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    _live_program(channel, exposure_policy="members_yt_site", asset=asset_ready)

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["gate_reason"] == "login"


def test_paused_takes_precedence_over_gate_reason(channel, asset_ready, monkeypatch):
    """休止中は gate_reason を出さない (paused の休止表示のみ、二重の理由を出さない)。"""
    fixed_now = datetime(2026, 7, 2, 4, 38, tzinfo=UTC)  # 13:38 JST (下記 windows 外=休止中)
    monkeypatch.setattr(timezone, "now", lambda: fixed_now)
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.broadcast_windows = [
        {"start": "06:00", "end": "10:00"},
        {"start": "16:00", "end": "24:00"},
    ]
    channel.save(update_fields=["cf_playback_hls_url", "broadcast_windows"])
    from scheduling.models import Program

    Program.objects.create(
        channel=channel,
        title="休止中でも解決される現在番組",
        type="recorded",
        asset=asset_ready,
        start_at=fixed_now - timedelta(minutes=10),
        end_at=fixed_now + timedelta(minutes=20),
        public_visible=True,
        exposure_policy="site_members",
    )

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"] is None
    assert d["paused"] is True
    assert d["gate_reason"] == ""


def test_no_current_program_defaults_to_public(channel):
    """編成 Program が無い (再放送フィラー中/オフライン) は常に public 扱い。"""
    channel.cf_playback_hls_url = "https://tv.example/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])

    d = Client().get("/api/v1/channels/ch1").json()
    assert d["hls_url"].startswith("https://tv.example/hls2/ch1/master.m3u8?token=")
    assert d["gate_reason"] == ""


def test_hls_auth_accepts_freshly_signed_token(channel):
    from core.hls_auth import sign_hls_token

    token = sign_hls_token(channel.slug)
    resp = Client().get(f"/api/v1/hls-auth?channel={channel.slug}&token={token}")
    assert resp.status_code == 200


def test_hls_auth_rejects_wrong_channel(channel):
    from core.hls_auth import sign_hls_token

    token = sign_hls_token(channel.slug)
    resp = Client().get(f"/api/v1/hls-auth?channel=other-channel&token={token}")
    assert resp.status_code == 403


def test_hls_auth_rejects_expired_token(channel):
    from core.hls_auth import sign_hls_token

    token = sign_hls_token(channel.slug, expires_sec=-1)
    resp = Client().get(f"/api/v1/hls-auth?channel={channel.slug}&token={token}")
    assert resp.status_code == 403


def test_hls_auth_rejects_missing_params(channel, db):
    resp = Client().get("/api/v1/hls-auth")
    assert resp.status_code == 403


# ---- エッジ (Cloudflare Worker) と共有する署名鍵・TTL (#27) ----


def test_signing_key_is_separable_from_secret_key(channel, settings):
    """ICSTV_HLS_SIGNING_KEY を設定したら SECRET_KEY ではなくそちらで署名する。

    Worker 側の secret が漏れても Django の SECRET_KEY (セッション/CSRF/暗号化フィールドの根)
    まで巻き込まないための分離。
    """
    from core.hls_auth import sign_hls_token, verify_hls_token

    settings.ICSTV_HLS_SIGNING_KEY = "edge-key-a"
    token = sign_hls_token(channel.slug)
    assert verify_hls_token(channel.slug, token) is True

    settings.ICSTV_HLS_SIGNING_KEY = "edge-key-b"  # 鍵を変えれば従来の署名は通らない
    assert verify_hls_token(channel.slug, token) is False


def test_signing_key_falls_back_to_secret_key(channel, settings):
    """未設定なら SECRET_KEY で署名する (Worker 未配線時の従来動作)。"""
    from core.hls_auth import _signature, sign_hls_token

    settings.ICSTV_HLS_SIGNING_KEY = ""
    token = sign_hls_token(channel.slug)
    expires_at, sig = token.split(".", 1)
    assert sig == _signature(channel.slug, int(expires_at))


def test_token_ttl_follows_settings(channel, settings):
    """TTL は settings 由来。エンタイトルメント変更がエッジへ効くまでの遅延がこの値になる。"""
    from django.utils import timezone

    from core.hls_auth import sign_hls_token

    settings.ICSTV_HLS_TOKEN_TTL_SEC = 120
    expires_at = int(sign_hls_token(channel.slug).split(".", 1)[0])
    remaining = expires_at - int(timezone.now().timestamp())
    assert 110 <= remaining <= 120
