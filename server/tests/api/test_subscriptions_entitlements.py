# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスク特典の適用: コメント会員バッジ・限定コンテンツゲート・プロフィール表示。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Comment, Member
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", nickname="みんと"):
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


@_PUBLIC_HOST
def test_comment_badge_for_subscriber(http_client, channel, db):
    sub_member = _member("sub@example.com", nickname="サブ会員")
    free_member = _member("free@example.com", nickname="無料会員")
    _subscribe(sub_member, feat_comment_perk=True)
    Comment.objects.create(channel=channel, member=sub_member, body="加入者コメント")
    Comment.objects.create(channel=channel, member=free_member, body="無料コメント")
    # #Phase1: バッジは島が API の badge フラグから描画する。
    items = http_client.get("/api/v1/channels/ch1/comments").json()["items"]
    assert sum(1 for c in items if c["badge"]) == 1  # comment_perk 会員の1件だけ


@_PUBLIC_HOST
def test_comment_badge_only_for_comment_perk(http_client, channel, db):
    # バッジは「有効サブスク」一般でなく comment_perk 特典に紐付く: 特典なしプランの会員には付かない。
    perk_member = _member("perk@example.com", nickname="特典会員")
    plain_member = _member("plain@example.com", nickname="一般サブスク")
    _subscribe(perk_member, feat_comment_perk=True)  # slug=matsu
    plan_hd = Plan.objects.create(name="梅", slug="ume", amount=480, rank=1, feat_hd=True)
    MemberSubscription.objects.create(
        member=plain_member,
        plan=plan_hd,
        status=SubStatus.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=30),
    )
    Comment.objects.create(channel=channel, member=perk_member, body="特典コメント")
    Comment.objects.create(channel=channel, member=plain_member, body="一般コメント")
    items = http_client.get("/api/v1/channels/ch1/comments").json()["items"]
    assert sum(1 for c in items if c["badge"]) == 1  # comment_perk の1件だけ


@_PUBLIC_HOST
def test_exclusive_requires_subscription(http_client, channel, db):
    _member()
    _login(http_client)
    # 未加入 → 加入ページへ
    res = http_client.get("/subscriptions/exclusive/")
    assert res.status_code == 302 and res.url == "/subscriptions/"


@_PUBLIC_HOST
def test_exclusive_accessible_for_subscriber(http_client, channel, db):
    m = _member()
    _subscribe(m, feat_exclusive=True)
    _login(http_client)
    res = http_client.get("/subscriptions/exclusive/")
    assert res.status_code == 200 and "会員限定コンテンツ" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_exclusive_lists_subscriber_vod(http_client, channel, asset_ready, db):
    # 会員限定ページ = サブスク限定の見逃し配信一覧 (feat_exclusive の実体)。
    from scheduling.models import Program, ProgramType, VodVisibility

    m = _member()
    _subscribe(m, feat_exclusive=True)
    _login(http_client)
    end = timezone.now() - timedelta(hours=2)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="限定見逃し番組",
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset_ready,
        vod_visibility=VodVisibility.SUBSCRIBERS,
    )
    body = http_client.get("/subscriptions/exclusive/").content.decode("utf-8")
    assert "限定見逃し番組" in body


@_PUBLIC_HOST
def test_exclusive_anon_redirected_to_login(http_client, db):
    res = http_client.get("/subscriptions/exclusive/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_plan_page_marks_unenforced_perks_as_soon(http_client, db):
    # 課金ページ: 未提供特典 (hd) は「準備中」と明示し、現行便益として宣伝しない。
    _member()
    Plan.objects.create(
        name="松",
        slug="matsu",
        amount=980,
        rank=3,
        feat_comment_perk=True,
        feat_hd=True,
        stripe_price_id="price_x",
    )
    _login(http_client)
    body = http_client.get("/subscriptions/").content.decode("utf-8")
    assert "コメント特典" in body  # 提供中は通常表示
    assert "高画質（準備中）" in body  # 未提供は準備中表示


@_PUBLIC_HOST
def test_profile_shows_subscriber_status(http_client, db):
    m = _member()
    _subscribe(m, feat_ad_free=True)
    _login(http_client)
    body = http_client.get("/members/").content.decode("utf-8")
    assert "加入中" in body
