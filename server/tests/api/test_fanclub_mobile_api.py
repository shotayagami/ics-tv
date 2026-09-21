# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""アプリ (Bearer トークン) 向けファンクラブ API (#MOBILE-02)。

/fc/<slug> (公開ページ) / 無料加入・退会 / ギフト償還 / 会員証一覧 / 会員限定チャット /
シリーズ投稿本文。web (SSR) と同じ fanclub.services を共有するため、資格判定そのものの
網羅は tests/api/test_fanclub_* に任せ、ここでは API 面 (auth 経路・ゲート時のステータス・
ロック時の漏えい防止) を検証する。CSRF を持てないアプリ経路の検証のため
Client(enforce_csrf_checks=True) を明示的に使う (test_mobile_comments.py と同じ作法)。
"""

from __future__ import annotations

from django.test import Client, override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorStatus,
    CreatorTier,
    FcChatMessage,
    MembershipStatus,
)
from members.models import Member
from scheduling.models import Series, SeriesPost, SeriesPostKind

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", *, verified=True):
    m = Member(
        email=email,
        nickname="みんと",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        email_verified_at=timezone.now() if verified else None,
    )
    m.set_password(_PW)
    m.save()
    return m


def _token_for(email="m@example.com") -> str:
    csrf_client = Client(enforce_csrf_checks=True)
    res = csrf_client.post(
        "/api/v1/auth/login",
        {"email": email, "password": _PW},
        content_type="application/json",
    )
    return res.json()["token"]


def _auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _creator(slug="circle-a", *, chat_required_level=0):
    c = Creator.objects.create(name="サークルA", slug=slug, chat_required_level=chat_required_level)
    fc_services.ensure_free_tier(c)
    return c


def _series(channel, creator, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


# ---- 公開ページ /fc/<slug> ----


@_PUBLIC_HOST
def test_fc_page_anonymous(channel, db):
    c = _creator()
    s = _series(channel, c)
    SeriesPost.objects.create(
        series=s,
        kind=SeriesPostKind.ARTICLE,
        title="限定お知らせ",
        body="秘密",
        is_published=True,
        fc_required_level=0,
    )
    client = Client(enforce_csrf_checks=True)

    res = client.get(f"/api/v1/fc/{c.slug}")

    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "サークルA"
    assert data["is_member"] is False
    assert data["viewer"] is None
    assert [t["action"] for t in data["tiers"]] == ["free_join"]
    assert data["posts"][0]["locked"] is True
    assert "秘密" not in res.content.decode()
    assert data["web_url"].endswith(f"/fc/{c.slug}/")


@_PUBLIC_HOST
def test_fc_page_suspended_creator_is_404(db):
    c = _creator()
    Creator.objects.filter(pk=c.pk).update(status=CreatorStatus.SUSPENDED)
    res = Client().get(f"/api/v1/fc/{c.slug}")
    assert res.status_code == 404


@_PUBLIC_HOST
def test_fc_page_with_bearer_member_has_viewer(channel, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.get(f"/api/v1/fc/{c.slug}", **_auth(token))

    assert res.status_code == 200
    data = res.json()
    assert data["is_member"] is True
    assert data["viewer"]["tier_level"] == 0
    assert data["viewer"]["member_no"] == "00001"
    assert data["tiers"][0]["action"] == "current"
    assert data["can_chat"] is True


# ---- 無料加入 / 退会 ----


@_PUBLIC_HOST
def test_join_free_with_bearer_needs_no_csrf(db):
    c = _creator()
    _member()
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.post(f"/api/v1/fc/{c.slug}/join", **_auth(token))

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert CreatorMembership.objects.filter(creator=c, status=MembershipStatus.ACTIVE).exists()


@_PUBLIC_HOST
def test_join_requires_login(db):
    c = _creator()
    res = Client(enforce_csrf_checks=True).post(f"/api/v1/fc/{c.slug}/join")
    assert res.status_code in (401, 403)  # Bearer 無し→cookie 経路の CSRF 403 もあり得る
    assert not CreatorMembership.objects.exists()


@_PUBLIC_HOST
def test_leave_free_membership(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.post(f"/api/v1/fc/{c.slug}/leave", **_auth(token))

    assert res.status_code == 200
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.LEFT


@_PUBLIC_HOST
def test_leave_paid_subscription_is_409(db):
    """Stripe サブスク在籍は DB を触らず web の解約導線へ誘導する。"""
    c = _creator()
    t1 = CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    m = _member()
    fc_services.join_free_tier(m, c)
    CreatorMembership.objects.filter(member=m, creator=c).update(
        tier=t1, stripe_subscription_id="sub_123"
    )
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.post(f"/api/v1/fc/{c.slug}/leave", **_auth(token))

    assert res.status_code == 409
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.ACTIVE


# ---- 会員証一覧 /members/fanclub ----


@_PUBLIC_HOST
def test_my_fanclub_lists_memberships(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    res = client.get("/api/v1/members/fanclub", **_auth(token))

    assert res.status_code == 200
    data = res.json()
    assert len(data["memberships"]) == 1
    ms = data["memberships"][0]
    assert ms["creator_slug"] == c.slug and ms["member_no"] == "00001"


@_PUBLIC_HOST
def test_my_fanclub_requires_login(db):
    res = Client(enforce_csrf_checks=True).get("/api/v1/members/fanclub")
    assert res.status_code == 401


# ---- 会員限定チャット ----


@_PUBLIC_HOST
def test_chat_list_403_for_non_member(db):
    c = _creator()
    _member()
    token = _token_for()
    res = Client(enforce_csrf_checks=True).get(f"/api/v1/fc/{c.slug}/chat", **_auth(token))
    assert res.status_code == 403


@_PUBLIC_HOST
def test_chat_post_and_list_with_bearer(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    token = _token_for()
    client = Client(enforce_csrf_checks=True)

    posted = client.post(
        f"/api/v1/fc/{c.slug}/chat",
        {"body": "こんにちは"},
        content_type="application/json",
        **_auth(token),
    )
    assert posted.status_code == 200
    assert posted.json()["body"] == "こんにちは"

    listed = client.get(f"/api/v1/fc/{c.slug}/chat", **_auth(token))
    assert listed.status_code == 200
    data = listed.json()
    assert data["can_post"] is True and data["me_member_id"] == m.pk
    assert [x["body"] for x in data["items"]] == ["こんにちは"]


@_PUBLIC_HOST
def test_chat_post_unverified_is_403(db):
    c = _creator()
    m = _member(verified=False)
    fc_services.join_free_tier(m, c)
    token = _token_for()

    res = Client(enforce_csrf_checks=True).post(
        f"/api/v1/fc/{c.slug}/chat",
        {"body": "hi"},
        content_type="application/json",
        **_auth(token),
    )

    assert res.status_code == 403
    assert not FcChatMessage.objects.exists()


@_PUBLIC_HOST
def test_chat_post_cooldown_is_429(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    FcChatMessage.objects.create(creator=c, member=m, body="直前の発言")
    token = _token_for()

    res = Client(enforce_csrf_checks=True).post(
        f"/api/v1/fc/{c.slug}/chat",
        {"body": "連投"},
        content_type="application/json",
        **_auth(token),
    )

    assert res.status_code == 429
    assert FcChatMessage.objects.count() == 1


@_PUBLIC_HOST
def test_chat_delete_own_only(db):
    c = _creator()
    other = _member("other@example.com")
    fc_services.join_free_tier(other, c)
    me = _member("me@example.com")
    fc_services.join_free_tier(me, c)
    others_msg = FcChatMessage.objects.create(creator=c, member=other, body="他人の")
    my_msg = FcChatMessage.objects.create(creator=c, member=me, body="自分の")
    token = _token_for("me@example.com")
    client = Client(enforce_csrf_checks=True)

    denied = client.delete(f"/api/v1/fc/{c.slug}/chat/{others_msg.pk}", **_auth(token))
    assert denied.status_code == 403

    ok = client.delete(f"/api/v1/fc/{c.slug}/chat/{my_msg.pk}", **_auth(token))
    assert ok.status_code == 200
    my_msg.refresh_from_db()
    assert my_msg.deleted_at is not None


# ---- シリーズ投稿本文 ----


@_PUBLIC_HOST
def test_series_post_detail_locked_hides_body(channel, db):
    c = _creator()
    s = _series(channel, c)
    post = SeriesPost.objects.create(
        series=s,
        kind=SeriesPostKind.ARTICLE,
        title="限定お知らせ",
        body="ここに秘密の本文があります",
        is_published=True,
        fc_required_level=0,
    )
    res = Client().get(f"/api/v1/series/{s.pk}/posts/{post.pk}")

    assert res.status_code == 200
    data = res.json()
    assert data["locked"] is True and data["gate"] == "login"
    assert data["fc_creator_slug"] == c.slug
    assert data["body_html"] == ""
    assert "秘密" not in res.content.decode()


@_PUBLIC_HOST
def test_series_post_detail_unlocked_for_joined_member(channel, db):
    c = _creator()
    s = _series(channel, c)
    post = SeriesPost.objects.create(
        series=s,
        kind=SeriesPostKind.ARTICLE,
        title="限定お知らせ",
        body="ここに秘密の本文があります",
        is_published=True,
        fc_required_level=0,
    )
    m = _member()
    fc_services.join_free_tier(m, c)
    token = _token_for()

    res = Client(enforce_csrf_checks=True).get(
        f"/api/v1/series/{s.pk}/posts/{post.pk}", **_auth(token)
    )

    assert res.status_code == 200
    data = res.json()
    assert data["locked"] is False and data["gate"] == ""
    assert data["body_html"] == "ここに秘密の本文があります"


# ---- 番組詳細のファンクラブ導線 (fc_creator_slug) ----


@_PUBLIC_HOST
def test_program_detail_exposes_fc_creator_slug(channel, asset_ready, db):
    from datetime import timedelta

    from scheduling.models import Program, ProgramType

    c = _creator()
    s = _series(channel, c)
    start = timezone.now() + timedelta(hours=1)
    p = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組X",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset_ready,
        public_visible=True,
        series=s,
    )
    no_creator = Series.objects.create(channel=channel, title="紐付け無し")
    # 同一チャンネルは時間帯重複不可 (program_no_overlap_per_channel) のため窓をずらす
    p2 = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組Y",
        start_at=start + timedelta(hours=2),
        end_at=start + timedelta(hours=3),
        asset=asset_ready,
        public_visible=True,
        series=no_creator,
    )
    client = Client()

    assert client.get(f"/api/v1/program/{p.pk}").json()["fc_creator_slug"] == c.slug
    assert client.get(f"/api/v1/program/{p2.pk}").json()["fc_creator_slug"] == ""

    # suspended creator はページごと 404 になるため導線も出さない
    Creator.objects.filter(pk=c.pk).update(status=CreatorStatus.SUSPENDED)
    assert client.get(f"/api/v1/program/{p.pk}").json()["fc_creator_slug"] == ""
