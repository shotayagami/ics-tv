# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FC有料ティア間の変更 (#27 §3.1 Must「アップグレード/ダウングレード」)。

アップグレード=即時+日割り / ダウングレード=期末適用 (F5)。Stripe API は monkeypatch する
(test_fanclub_stripe.py と同じ思想)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub import stripe_gateway as gw
from fanclub import webhook as wh
from fanclub.models import Creator, CreatorMembership, CreatorTier, MembershipStatus
from members.models import Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a"):
    c = Creator.objects.create(
        name="サークルA",
        slug=slug,
        stripe_connect_account_id="acct_test",
        stripe_connect_onboarded=True,
    )
    fc_services.ensure_free_tier(c)
    return c


def _tier(creator, *, level, price_minor, price_id):
    return CreatorTier.objects.create(
        creator=creator,
        level=level,
        name=f"ティア{level}",
        price_minor=price_minor,
        stripe_price_id=price_id,
    )


def _paid_membership(member, creator, tier):
    return CreatorMembership.objects.create(
        member=member,
        creator=creator,
        tier=tier,
        status=MembershipStatus.ACTIVE,
        joined_at=timezone.now(),
        stripe_customer_id="cus_x",
        stripe_subscription_id="sub_x",
    )


def _linked_series(channel, creator, title="番組A"):
    from fanclub.models import CreatorSeriesLink
    from scheduling.models import Series

    series = Series.objects.create(channel=channel, title=title)
    CreatorSeriesLink.objects.create(series=series, creator=creator)
    return series


def _fixture():
    """creator + 2段の有料ティア + level1 に在籍中の有料会員。"""
    c = _creator()
    t1 = _tier(c, level=1, price_minor=500, price_id="price_l1")
    t2 = _tier(c, level=2, price_minor=1500, price_id="price_l2")
    m = _member()
    return c, t1, t2, m, _paid_membership(m, c, t1)


# ---- active_paid_membership ----


def test_active_paid_membership_ignores_free_tier(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    assert fc_services.active_paid_membership(m, c) is None


def test_active_paid_membership_requires_subscription_id(db):
    c, _t1, _t2, m, membership = _fixture()
    assert fc_services.active_paid_membership(m, c) is not None
    membership.stripe_subscription_id = ""
    membership.save(update_fields=["stripe_subscription_id"])
    assert fc_services.active_paid_membership(m, c) is None


# ---- plan_tier_change ----


def test_plan_tier_change_direction(db):
    _c, t1, t2, _m, membership = _fixture()
    assert fc_services.plan_tier_change(membership, t2) == "upgrade"
    membership.tier = t2
    assert fc_services.plan_tier_change(membership, t1) == "downgrade"


def test_plan_tier_change_rejects_same_tier(db):
    _c, t1, _t2, _m, membership = _fixture()
    try:
        fc_services.plan_tier_change(membership, t1)
        raised = False
    except fc_services.TierChangeNotAllowedError:
        raised = True
    assert raised


def test_plan_tier_change_rejects_free_tier(db):
    c, _t1, _t2, _m, membership = _fixture()
    free = CreatorTier.objects.get(creator=c, level=0)
    try:
        fc_services.plan_tier_change(membership, free)
        raised = False
    except fc_services.TierChangeNotAllowedError:
        raised = True
    assert raised


def test_plan_tier_change_rejects_other_creator_tier(db):
    _c, _t1, _t2, _m, membership = _fixture()
    other = _creator(slug="circle-b")
    other_tier = _tier(other, level=2, price_minor=800, price_id="price_other")
    try:
        fc_services.plan_tier_change(membership, other_tier)
        raised = False
    except fc_services.TierChangeNotAllowedError:
        raised = True
    assert raised


def test_plan_tier_change_rejects_not_joinable_tier(db):
    _c, _t1, t2, _m, membership = _fixture()
    t2.stripe_price_id = ""  # Price 未設定 = 準備中
    t2.save(update_fields=["stripe_price_id"])
    try:
        fc_services.plan_tier_change(membership, t2)
        raised = False
    except fc_services.TierNotJoinableError:
        raised = True
    assert raised


# ---- change_tier ----


def test_change_tier_upgrade_is_immediate(db, monkeypatch):
    _c, _t1, t2, _m, membership = _fixture()
    calls = []
    monkeypatch.setattr(gw, "change_subscription_price_now", lambda **kw: calls.append(kw) or None)
    direction, effective_at = fc_services.change_tier(membership, t2)
    assert direction == "upgrade" and effective_at is None
    assert calls == [{"subscription_id": "sub_x", "new_price_id": "price_l2"}]
    membership.refresh_from_db()
    assert membership.tier_id == t2.id
    assert membership.pending_tier_id is None


def test_change_tier_downgrade_is_scheduled_at_period_end(db, monkeypatch):
    _c, t1, t2, _m, membership = _fixture()
    membership.tier = t2
    membership.save(update_fields=["tier"])
    epoch = 1_800_000_000
    monkeypatch.setattr(gw, "schedule_subscription_price_at_period_end", lambda **kw: epoch)
    direction, effective_at = fc_services.change_tier(membership, t1)
    assert direction == "downgrade"
    assert effective_at == datetime.fromtimestamp(epoch, tz=UTC)
    membership.refresh_from_db()
    # 期末までは現在のティアを維持し、予約だけを記録する
    assert membership.tier_id == t2.id
    assert membership.pending_tier_id == t1.id
    assert membership.pending_tier_effective_at == effective_at


def test_change_tier_upgrade_clears_pending_downgrade(db, monkeypatch):
    _c, t1, t2, _m, membership = _fixture()
    membership.pending_tier = t1
    membership.pending_tier_effective_at = timezone.now()
    membership.save(update_fields=["pending_tier", "pending_tier_effective_at"])
    monkeypatch.setattr(gw, "change_subscription_price_now", lambda **kw: None)
    fc_services.change_tier(membership, t2)
    membership.refresh_from_db()
    assert membership.pending_tier_id is None
    assert membership.pending_tier_effective_at is None


def test_change_tier_does_not_touch_db_when_stripe_fails(db, monkeypatch):
    _c, t1, t2, _m, membership = _fixture()

    def boom(**kw):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(gw, "change_subscription_price_now", boom)
    try:
        fc_services.change_tier(membership, t2)
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    membership.refresh_from_db()
    assert membership.tier_id == t1.id


def test_cancel_tier_change_is_idempotent(db, monkeypatch):
    _c, t1, _t2, _m, membership = _fixture()
    released = []
    monkeypatch.setattr(
        gw, "release_subscription_schedule", lambda **kw: released.append(kw) or True
    )
    assert fc_services.cancel_tier_change(membership) is False  # 予約なし
    assert not released
    membership.pending_tier = t1
    membership.pending_tier_effective_at = timezone.now()
    membership.save(update_fields=["pending_tier", "pending_tier_effective_at"])
    assert fc_services.cancel_tier_change(membership) is True
    assert released == [{"subscription_id": "sub_x"}]
    membership.refresh_from_db()
    assert membership.pending_tier_id is None


# ---- ビュー ----


@_PUBLIC_HOST
def test_change_confirm_renders_for_paid_member(http_client, db):
    c, _t1, t2, _m, _membership = _fixture()
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t2.id}/change/confirm/?next=/series/1/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "プラン変更の確認" in body and "日割り" in body
    assert f"/fanclub/{c.slug}/tiers/{t2.id}/change/" in body


@_PUBLIC_HOST
def test_change_confirm_downgrade_states_period_end(http_client, db):
    c, t1, t2, _m, membership = _fixture()
    membership.tier = t2
    membership.save(update_fields=["tier"])
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t1.id}/change/confirm/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "請求は発生しません" in body


@_PUBLIC_HOST
def test_change_confirm_rejects_free_member(http_client, db):
    c = _creator()
    t2 = _tier(c, level=2, price_minor=1500, price_id="price_l2")
    m = _member()
    fc_services.join_free_tier(m, c)
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t2.id}/change/confirm/", follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("有料プランをご利用中の方のみ" in x for x in msgs)


@_PUBLIC_HOST
def test_change_post_upgrade_reports_proration(http_client, db, monkeypatch):
    c, _t1, t2, _m, membership = _fixture()
    _login(http_client)
    monkeypatch.setattr(gw, "change_subscription_price_now", lambda **kw: None)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t2.id}/change/", follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("日割り" in x for x in msgs)
    membership.refresh_from_db()
    assert membership.tier_id == t2.id


@_PUBLIC_HOST
def test_change_post_downgrade_reports_effective_date(http_client, db, monkeypatch):
    c, t1, t2, _m, membership = _fixture()
    membership.tier = t2
    membership.save(update_fields=["tier"])
    _login(http_client)
    monkeypatch.setattr(gw, "schedule_subscription_price_at_period_end", lambda **kw: 1_800_000_000)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t1.id}/change/", follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("から「ティア1」へ変更されます" in x for x in msgs)


@_PUBLIC_HOST
def test_change_post_503_when_stripe_unconfigured(http_client, db):
    c, _t1, t2, _m, _membership = _fixture()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t2.id}/change/")
    assert res.status_code == 503


@_PUBLIC_HOST
def test_change_post_anonymous_redirects_to_login(http_client, db):
    c, _t1, t2, _m, _membership = _fixture()
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t2.id}/change/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_change_cancel_clears_reservation(http_client, db, monkeypatch):
    c, t1, t2, _m, membership = _fixture()
    membership.tier = t2
    membership.pending_tier = t1
    membership.pending_tier_effective_at = timezone.now()
    membership.save(update_fields=["tier", "pending_tier", "pending_tier_effective_at"])
    _login(http_client)
    monkeypatch.setattr(gw, "release_subscription_schedule", lambda **kw: True)
    res = http_client.post(f"/fanclub/{c.slug}/change/cancel/", follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("予約を取り消しました" in x for x in msgs)
    membership.refresh_from_db()
    assert membership.pending_tier_id is None


@_PUBLIC_HOST
def test_change_cancel_without_reservation_is_noop(http_client, db, monkeypatch):
    c, _t1, _t2, _m, _membership = _fixture()
    _login(http_client)
    called = []
    monkeypatch.setattr(gw, "release_subscription_schedule", lambda **kw: called.append(1) or True)
    res = http_client.post(f"/fanclub/{c.slug}/change/cancel/")
    assert res.status_code == 302
    assert not called


# ---- シリーズ詳細の導線 ----


@_PUBLIC_HOST
def test_series_detail_offers_change_links_to_paid_member(http_client, channel, db):
    c, t1, t2, _m, _membership = _fixture()
    series = _linked_series(channel, c)
    _login(http_client)
    res = http_client.get(f"/series/{series.pk}/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    # 在籍中の level1 は「現在のプラン」、上位の level2 は変更導線になる
    assert "現在のプラン" in body
    assert f"/tiers/{t2.id}/change/confirm/" in body
    assert f"/tiers/{t1.id}/confirm/" not in body


@_PUBLIC_HOST
def test_series_detail_offers_downgrade_link(http_client, channel, db):
    c, t1, t2, _m, membership = _fixture()
    membership.tier = t2
    membership.save(update_fields=["tier"])
    series = _linked_series(channel, c)
    _login(http_client)
    res = http_client.get(f"/series/{series.pk}/")
    body = res.content.decode("utf-8")
    # 下位ティアも隠さずダウングレード導線として出す (旧実装は現レベル以下を隠していた)
    assert f"/tiers/{t1.id}/change/confirm/" in body
    assert "ダウングレード" in body


# ---- Webhook: Price から在籍ティアを追従させる ----


def _sub_event(member, creator, *, evt_id, price_id, created=1000):
    return {
        "id": evt_id,
        "created": created,
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_x",
                "customer": "cus_x",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_end": int(timezone.now().timestamp()) + 86400,
                "metadata": {"member_id": str(member.pk), "creator_id": str(creator.pk)},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


def test_webhook_follows_tier_change_by_price(db):
    c, _t1, t2, m, membership = _fixture()
    wh.handle_event(_sub_event(m, c, evt_id="evt_up", price_id="price_l2"))
    membership.refresh_from_db()
    assert membership.tier_id == t2.id


def test_webhook_clears_pending_when_scheduled_tier_applies(db):
    c, t1, t2, m, membership = _fixture()
    membership.tier = t2
    membership.pending_tier = t1
    membership.pending_tier_effective_at = timezone.now()
    membership.save(update_fields=["tier", "pending_tier", "pending_tier_effective_at"])
    wh.handle_event(_sub_event(m, c, evt_id="evt_boundary", price_id="price_l1"))
    membership.refresh_from_db()
    assert membership.tier_id == t1.id
    assert membership.pending_tier_id is None
    assert membership.pending_tier_effective_at is None


def test_webhook_keeps_tier_for_unknown_price(db):
    c, t1, _t2, m, membership = _fixture()
    wh.handle_event(_sub_event(m, c, evt_id="evt_unknown", price_id="price_not_ours"))
    membership.refresh_from_db()
    assert membership.tier_id == t1.id


def test_webhook_keeps_pending_until_boundary(db):
    """ダウングレード予約直後に来る subscription.updated では予約を消さない。"""
    c, t1, t2, m, membership = _fixture()
    membership.tier = t2
    membership.pending_tier = t1
    membership.pending_tier_effective_at = timezone.now()
    membership.save(update_fields=["tier", "pending_tier", "pending_tier_effective_at"])
    wh.handle_event(_sub_event(m, c, evt_id="evt_still_l2", price_id="price_l2"))
    membership.refresh_from_db()
    assert membership.tier_id == t2.id
    assert membership.pending_tier_id == t1.id
