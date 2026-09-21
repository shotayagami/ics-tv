# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスク コア: エンタイトルメント解決 + 有料ゲートデコレータ (Stripe 非依存)。"""

from __future__ import annotations

from datetime import timedelta

from django.test import RequestFactory
from django.utils import timezone

from members.models import Member
from subscriptions import services
from subscriptions.decorators import subscription_required
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _plan(slug="matsu", **feats):
    return Plan.objects.create(
        name="松", slug=slug, amount=980, rank=3, stripe_price_id="price_x", **feats
    )


def _sub(member, plan, status=SubStatus.ACTIVE, period_days=30):
    return MemberSubscription.objects.create(
        member=member,
        plan=plan,
        status=status,
        current_period_end=timezone.now() + timedelta(days=period_days),
        stripe_subscription_id="sub_x",
    )


def test_plan_features_property(db):
    p = _plan(feat_ad_free=True, feat_hd=True)
    assert p.features == {"ad_free", "hd"}


def test_entitlements_active_sub(db):
    m = _member()
    p = _plan(feat_ad_free=True, feat_comment_perk=True)
    _sub(m, p)
    assert services.entitlements(m) == {"ad_free", "comment_perk"}
    assert services.has_active_subscription(m) is True


def test_entitlements_none_when_no_sub(db):
    m = _member()
    assert services.entitlements(m) == set()
    assert services.has_active_subscription(m) is False


def test_entitlements_empty_when_canceled(db):
    m = _member()
    p = _plan(feat_ad_free=True)
    _sub(m, p, status=SubStatus.CANCELED)
    assert services.entitlements(m) == set()
    assert services.has_active_subscription(m) is False


def test_entitlements_empty_when_period_expired(db):
    m = _member()
    p = _plan(feat_hd=True)
    _sub(m, p, status=SubStatus.ACTIVE, period_days=-1)  # 期限切れ
    assert services.has_active_subscription(m) is False
    assert services.entitlements(m) == set()


def test_entitlements_none_for_anonymous(db):
    assert services.entitlements(None) == set()
    assert services.has_active_subscription(None) is False


def test_subscriber_member_ids(db):
    m1 = _member("a@example.com")
    m2 = _member("b@example.com")
    p = _plan()
    _sub(m1, p, status=SubStatus.ACTIVE)
    _sub(m2, p, status=SubStatus.CANCELED)
    assert services.subscriber_member_ids([m1.pk, m2.pk, None]) == {m1.pk}


def test_comment_perk_member_ids(db):
    # バッジは「有効サブスク」一般でなく comment_perk 特典に紐付く。
    m1 = _member("a@example.com")
    m2 = _member("b@example.com")
    m3 = _member("c@example.com")
    perk = _plan(slug="perk", feat_comment_perk=True)
    nope = _plan(slug="nope", feat_hd=True)  # サブスクだが comment_perk なし
    _sub(m1, perk, status=SubStatus.ACTIVE)
    _sub(m2, nope, status=SubStatus.ACTIVE)
    _sub(m3, perk, status=SubStatus.CANCELED)  # comment_perk だが無効サブスク
    assert services.comment_perk_member_ids([m1.pk, m2.pk, m3.pk, None]) == {m1.pk}


def test_plan_perk_list_marks_unenforced_as_soon(db):
    # 提供中の特典 (comment_perk) は active=True、未提供 (hd/ad_free/exclusive) は準備中=False。
    p = _plan(
        slug="all",
        feat_comment_perk=True,
        feat_hd=True,
        feat_ad_free=True,
        feat_exclusive=True,
    )
    active = dict(p.perk_list())
    comment_label = next(label for label in active if label.startswith("コメント特典"))
    assert active[comment_label] is True
    assert active["会員限定コンテンツ"] is True  # VOD subscribers 公開で実在
    assert active["高画質"] is False  # 準備中
    assert active["広告非表示"] is False  # 準備中


def test_subscription_required_decorator(db):
    from django.http import HttpResponse

    @subscription_required
    def view(request):
        return HttpResponse("ok")

    rf = RequestFactory()
    m = _member()

    # 未ログイン → login
    req = rf.get("/x/")
    req.member = None
    r = view(req)
    assert r.status_code == 302 and r.url.startswith("/members/login/")

    # ログイン済・未加入 → 加入ページ
    req2 = rf.get("/x/")
    req2.member = m
    r2 = view(req2)
    assert r2.status_code == 302 and r2.url == "/subscriptions/"

    # 加入済 → 通過
    _sub(m, _plan())
    req3 = rf.get("/x/")
    req3.member = m
    assert view(req3).status_code == 200
