# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FC有料ティア Stripe: Checkout(destination charge) / Portal / Webhook (#27 Phase B)。

実 API は monkeypatch する (subscriptions/test_subscriptions_stripe.py と同じ思想)。
"""

from __future__ import annotations

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub import stripe_gateway as gw
from fanclub import webhook as wh
from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorTier,
    FcProcessedStripeEvent,
    MembershipStatus,
)
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


def _creator(slug="circle-a", *, onboarded=True):
    c = Creator.objects.create(
        name="サークルA",
        slug=slug,
        stripe_connect_account_id="acct_test" if onboarded else "",
        stripe_connect_onboarded=onboarded,
    )
    fc_services.ensure_free_tier(c)
    return c


def _paid_tier(creator, *, level=1, price_id="price_abc", price_minor=500):
    return CreatorTier.objects.create(
        creator=creator,
        level=level,
        name="ベーシック",
        price_minor=price_minor,
        stripe_price_id=price_id,
    )


def _epoch_future(days=30):
    return int(timezone.now().timestamp()) + days * 86400


# ---- tier_is_joinable / fc_gate_reason ----


def test_tier_is_joinable_free_always_true(db):
    c = _creator(onboarded=False)
    free = CreatorTier.objects.get(creator=c, level=0)
    assert fc_services.tier_is_joinable(free) is True


def test_tier_is_joinable_paid_requires_price_and_onboarding(db):
    c = _creator(onboarded=False)
    t = _paid_tier(c, price_id="")
    assert fc_services.tier_is_joinable(t) is False  # price 無し
    t.stripe_price_id = "price_abc"
    t.save()
    assert fc_services.tier_is_joinable(t) is False  # 未オンボーディング
    c.stripe_connect_onboarded = True
    c.save()
    assert fc_services.tier_is_joinable(t) is True


def test_fc_gate_reason_unavailable_when_tier_not_joinable(db):
    c = _creator(onboarded=False)
    t = _paid_tier(c)  # price はあるが未オンボーディングでまだ joinable でない
    assert fc_services.fc_gate_reason(t.level, None, c) == "fc_unavailable"


def test_fc_gate_reason_join_when_tier_joinable(db):
    c = _creator(onboarded=True)
    t = _paid_tier(c)
    m = _member()
    assert fc_services.fc_gate_reason(t.level, m, c) == "fc_join"
    assert fc_services.fc_gate_reason(t.level, None, c) == "login"


# ---- join() は有料ティアを拒否する ----


def test_join_rejects_paid_tier(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    try:
        fc_services.join(m, c, t)
        raised = False
    except fc_services.TierNotJoinableError:
        raised = True
    assert raised


# ---- Checkout ----


@_PUBLIC_HOST
def test_checkout_creates_session(http_client, db, monkeypatch):
    c = _creator()
    t = _paid_tier(c)
    _member()
    _login(http_client)
    monkeypatch.setattr(gw, "ensure_customer", lambda member, membership: "cus_test")
    monkeypatch.setattr(gw, "create_checkout_session", lambda **kw: "https://checkout.stripe/test")
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t.id}/checkout/")
    assert res.status_code == 302 and res.url == "https://checkout.stripe/test"


@_PUBLIC_HOST
def test_checkout_rejects_not_joinable_tier(http_client, db):
    c = _creator(onboarded=False)
    t = _paid_tier(c)  # 未オンボーディングで joinable でない
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t.id}/checkout/", follow=True)
    msgs = [str(m) for m in res.context["messages"]]
    assert any("準備中" in m for m in msgs)


@_PUBLIC_HOST
def test_checkout_503_when_stripe_unconfigured(http_client, db):
    c = _creator()
    t = _paid_tier(c)
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t.id}/checkout/")
    assert res.status_code == 503


@_PUBLIC_HOST
def test_checkout_blocks_double_subscribe(http_client, db, monkeypatch):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _login(http_client)
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        stripe_subscription_id="sub_existing",
    )
    called = []
    monkeypatch.setattr(gw, "create_checkout_session", lambda **kw: called.append(1) or "x")
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t.id}/checkout/", follow=True)
    assert not called
    msgs = [str(x) for x in res.context["messages"]]
    assert any("すでに" in x for x in msgs)


@_PUBLIC_HOST
def test_checkout_anonymous_redirects_to_login(http_client, db):
    c = _creator()
    t = _paid_tier(c)
    res = http_client.post(f"/fanclub/{c.slug}/tiers/{t.id}/checkout/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


# ---- 特商法 申込み最終確認画面 (§5.2) ----


@_PUBLIC_HOST
def test_confirm_renders_when_joinable(http_client, db):
    c = _creator()
    t = _paid_tier(c)
    _member()
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t.id}/confirm/?next=/series/1/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "¥500" in body and "同意して申し込む" in body
    assert f"/fanclub/{c.slug}/tiers/{t.id}/checkout/" in body


@_PUBLIC_HOST
def test_confirm_rejects_not_joinable_tier(http_client, db):
    c = _creator(onboarded=False)
    t = _paid_tier(c)
    _member()
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t.id}/confirm/", follow=True)
    msgs = [str(m) for m in res.context["messages"]]
    assert any("準備中" in m for m in msgs)


@_PUBLIC_HOST
def test_confirm_rejects_double_subscribe(http_client, db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _login(http_client)
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        stripe_subscription_id="sub_existing",
    )
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t.id}/confirm/", follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("すでに" in x for x in msgs)


@_PUBLIC_HOST
def test_confirm_anonymous_redirects_to_login(http_client, db):
    c = _creator()
    t = _paid_tier(c)
    res = http_client.get(f"/fanclub/{c.slug}/tiers/{t.id}/confirm/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


# ---- Portal ----


@_PUBLIC_HOST
def test_portal_noop_without_membership(http_client, db):
    c = _creator()
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/portal/")
    assert res.status_code == 302 and res.url == "/"


@_PUBLIC_HOST
def test_portal_creates_session(http_client, db, monkeypatch):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _login(http_client)
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        stripe_customer_id="cus_x",
        stripe_subscription_id="sub_x",
    )
    monkeypatch.setattr(gw, "create_portal_session", lambda **kw: "https://portal.stripe/test")
    res = http_client.post(f"/fanclub/{c.slug}/portal/")
    assert res.status_code == 302 and res.url == "https://portal.stripe/test"


@_PUBLIC_HOST
def test_leave_redirects_paid_member_to_portal(http_client, db, monkeypatch):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _login(http_client)
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        stripe_customer_id="cus_x",
        stripe_subscription_id="sub_x",
    )
    monkeypatch.setattr(gw, "create_portal_session", lambda **kw: "https://portal.stripe/test")
    res = http_client.post(f"/fanclub/{c.slug}/leave/")
    assert res.status_code == 302 and res.url == "https://portal.stripe/test"
    # DB は Webhook のみが更新する規律のため、ここでは在籍のまま変化しない
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.ACTIVE


@_PUBLIC_HOST
def test_leave_still_works_directly_for_free_tier(http_client, db):
    c = _creator()
    fc_services.join_free_tier(_member(), c)
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/leave/")
    assert res.status_code == 302
    membership = CreatorMembership.objects.get(creator=c)
    assert membership.status == MembershipStatus.LEFT


# ---- Webhook ハンドラ (純関数・event dict) ----


def _checkout_event(member, creator, tier, *, customer="cus_1", subscription="sub_1"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {
                    "member_id": str(member.pk),
                    "creator_id": str(creator.pk),
                    "tier_id": str(tier.pk),
                },
                "customer": customer,
                "subscription": subscription,
            }
        },
    }


def test_webhook_checkout_completed_creates_membership(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(_checkout_event(m, c, t))
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.tier_id == t.id
    assert membership.stripe_customer_id == "cus_1"
    assert membership.stripe_subscription_id == "sub_1"
    assert membership.joined_at is not None


def _sub_event(member, creator, *, evt_id, status, created, etype, price_id="price_abc"):
    return {
        "id": evt_id,
        "created": created,
        "type": etype,
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": status,
                "cancel_at_period_end": False,
                "current_period_end": _epoch_future(),
                "metadata": {"member_id": str(member.pk), "creator_id": str(creator.pk)},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


def test_webhook_subscription_updated_syncs_period(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(_checkout_event(m, c, t))
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_1",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.current_period_end is not None


def test_webhook_subscription_deleted_marks_left(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(_checkout_event(m, c, t))
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_del",
            status="canceled",
            created=2000,
            etype="customer.subscription.deleted",
        )
    )
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.LEFT
    assert membership.left_at is not None


def test_webhook_subscription_event_before_checkout_is_noop(db):
    """順序前後(checkout.session.completed未着)は紐付け不能として静かに戻る。"""
    c = _creator()
    m = _member()
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_early",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    assert not CreatorMembership.objects.filter(member=m, creator=c).exists()


def test_webhook_idempotent_by_event_id(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(_checkout_event(m, c, t))
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_1",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_1",
            status="canceled",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.ACTIVE  # 同一event.id再送はスキップ
    assert FcProcessedStripeEvent.objects.filter(event_id="evt_1").count() == 1


def test_webhook_drops_stale_out_of_order_event(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(_checkout_event(m, c, t))
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_del",
            status="canceled",
            created=2000,
            etype="customer.subscription.deleted",
        )
    )
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.LEFT
    # 後着した古い active (created=1000) は stale として捨てる
    wh.handle_event(
        _sub_event(
            m,
            c,
            evt_id="evt_old",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.LEFT


def test_webhook_account_updated_syncs_onboarded(db):
    c = Creator.objects.create(
        name="サークルB", slug="circle-b", stripe_connect_account_id="acct_1"
    )
    wh.handle_event(
        {
            "type": "account.updated",
            "data": {
                "object": {"id": "acct_1", "charges_enabled": True, "details_submitted": True}
            },
        }
    )
    c.refresh_from_db()
    assert c.stripe_connect_onboarded is True


def test_webhook_account_updated_unknown_account_is_noop(db):
    # account_id に対応する Creator が無くても例外を出さず静かに戻る
    wh.handle_event(
        {
            "type": "account.updated",
            "data": {
                "object": {"id": "acct_unknown", "charges_enabled": True, "details_submitted": True}
            },
        }
    )


# ---- Webhook view (署名検証) ----


@_PUBLIC_HOST
def test_stripe_webhook_view_bad_signature_400(http_client, db, monkeypatch):
    def boom(payload, sig):
        raise ValueError("bad sig")

    monkeypatch.setattr(gw, "construct_event", boom)
    res = http_client.post("/fanclub/stripe/webhook/", data="{}", content_type="application/json")
    assert res.status_code == 400


@_PUBLIC_HOST
def test_stripe_webhook_view_valid_processes(http_client, db, monkeypatch):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    event = _checkout_event(m, c, t)
    monkeypatch.setattr(gw, "construct_event", lambda payload, sig: event)
    res = http_client.post("/fanclub/stripe/webhook/", data="{}", content_type="application/json")
    assert res.status_code == 200
    assert CreatorMembership.objects.get(member=m, creator=c).stripe_customer_id == "cus_1"
