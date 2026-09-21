# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスク Stripe: 会員ページ / Checkout / Portal / Webhook (実 API は monkeypatch)。"""

from __future__ import annotations

from django.test import override_settings
from django.utils import timezone

from members.models import Member
from subscriptions import stripe_gateway as gw
from subscriptions import webhook as wh
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


def _plan(slug="matsu", price="price_123", **feats):
    return Plan.objects.create(
        name="松", slug=slug, amount=980, rank=3, stripe_price_id=price, **feats
    )


def _epoch_future(days=30):
    return int(timezone.now().timestamp()) + days * 86400


# ---- 会員ページ / Checkout / Portal ----
@_PUBLIC_HOST
def test_page_requires_login(http_client, db):
    res = http_client.get("/subscriptions/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_page_lists_plans(http_client, db):
    _member()
    _login(http_client)
    _plan(feat_ad_free=True)
    res = http_client.get("/subscriptions/")
    assert res.status_code == 200 and "松" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_checkout_creates_session_and_customer(http_client, db, monkeypatch):
    m = _member()
    _login(http_client)
    _plan(slug="matsu")
    monkeypatch.setattr(gw, "ensure_customer", lambda member, sub: "cus_test")
    monkeypatch.setattr(gw, "create_checkout_session", lambda **kw: "https://checkout.stripe/test")
    res = http_client.post("/subscriptions/checkout/matsu/")
    assert res.status_code == 302 and res.url == "https://checkout.stripe/test"
    assert MemberSubscription.objects.get(member=m).stripe_customer_id == "cus_test"


@_PUBLIC_HOST
def test_checkout_503_when_stripe_unconfigured(http_client, db):
    _member()
    _login(http_client)
    _plan(slug="matsu")  # price はあるが STRIPE_SECRET_KEY 空 → ensure_customer が raise
    res = http_client.post("/subscriptions/checkout/matsu/")
    assert res.status_code == 503


@_PUBLIC_HOST
def test_portal_redirects_to_page_without_sub(http_client, db):
    _member()
    _login(http_client)
    res = http_client.post("/subscriptions/portal/")
    assert res.status_code == 302 and res.url == "/subscriptions/"


@_PUBLIC_HOST
def test_portal_creates_session(http_client, db, monkeypatch):
    m = _member()
    _login(http_client)
    MemberSubscription.objects.create(member=m, stripe_customer_id="cus_x", status=SubStatus.ACTIVE)
    monkeypatch.setattr(gw, "create_portal_session", lambda **kw: "https://portal.stripe/test")
    res = http_client.post("/subscriptions/portal/")
    assert res.status_code == 302 and res.url == "https://portal.stripe/test"


# ---- Webhook ハンドラ (純関数・event dict) ----
def test_webhook_checkout_completed_links_sub(db):
    m = _member()
    wh.handle_event(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"member_id": str(m.pk)},
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        }
    )
    s = MemberSubscription.objects.get(member=m)
    assert s.stripe_customer_id == "cus_1" and s.stripe_subscription_id == "sub_1"


def test_webhook_subscription_updated_syncs(db):
    m = _member()
    p = _plan(slug="matsu", price="price_abc", feat_hd=True)
    MemberSubscription.objects.create(member=m, stripe_subscription_id="sub_1")
    fut = _epoch_future()
    wh.handle_event(
        {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_end": fut,
                    "metadata": {"member_id": str(m.pk)},
                    "items": {"data": [{"price": {"id": "price_abc"}}]},
                }
            },
        }
    )
    s = MemberSubscription.objects.get(member=m)
    assert s.status == SubStatus.ACTIVE and s.plan == p and s.is_active


def test_webhook_subscription_deleted_marks_canceled(db):
    m = _member()
    MemberSubscription.objects.create(
        member=m, stripe_subscription_id="sub_1", status=SubStatus.ACTIVE
    )
    wh.handle_event(
        {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_1",
                    "status": "canceled",
                    "metadata": {"member_id": str(m.pk)},
                    "items": {"data": []},
                }
            },
        }
    )
    s = MemberSubscription.objects.get(member=m)
    assert s.status == SubStatus.CANCELED and not s.is_active


# ---- Webhook 冪等 (event.id) + 順序 (event.created) ガード (#sec L-3) ----
def _sub_evt(m, *, evt_id, status, created, etype="customer.subscription.updated"):
    return {
        "id": evt_id,
        "created": created,
        "type": etype,
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": status,
                "current_period_end": _epoch_future(),
                "metadata": {"member_id": str(m.pk)},
                "items": {"data": [{"price": {"id": "price_abc"}}]},
            }
        },
    }


def test_webhook_idempotent_by_event_id(db):
    from subscriptions.models import ProcessedStripeEvent

    m = _member()
    _plan(slug="matsu", price="price_abc")
    MemberSubscription.objects.create(member=m, stripe_subscription_id="sub_1")
    wh.handle_event(_sub_evt(m, evt_id="evt_1", status="active", created=1000))
    assert MemberSubscription.objects.get(member=m).status == SubStatus.ACTIVE
    # 同一 event.id の再送は payload が canceled でもスキップ (冪等)
    wh.handle_event(_sub_evt(m, evt_id="evt_1", status="canceled", created=1000))
    assert MemberSubscription.objects.get(member=m).status == SubStatus.ACTIVE
    assert ProcessedStripeEvent.objects.filter(event_id="evt_1").count() == 1


def test_webhook_drops_stale_out_of_order_event(db):
    m = _member()
    _plan(slug="matsu", price="price_abc")
    MemberSubscription.objects.create(
        member=m, stripe_subscription_id="sub_1", status=SubStatus.ACTIVE
    )
    # 新しい削除イベント (created=2000) → canceled
    wh.handle_event(
        _sub_evt(
            m,
            evt_id="evt_del",
            status="canceled",
            created=2000,
            etype="customer.subscription.deleted",
        )
    )
    assert MemberSubscription.objects.get(member=m).status == SubStatus.CANCELED
    # 後着した古い active (created=1000) は stale として捨てる (特典が復活しない)
    wh.handle_event(_sub_evt(m, evt_id="evt_old", status="active", created=1000))
    assert MemberSubscription.objects.get(member=m).status == SubStatus.CANCELED
    # さらに新しい active (created=3000) は適用される
    wh.handle_event(_sub_evt(m, evt_id="evt_new", status="active", created=3000))
    assert MemberSubscription.objects.get(member=m).status == SubStatus.ACTIVE


# ---- Webhook view (署名検証) ----
@_PUBLIC_HOST
def test_webhook_view_bad_signature_400(http_client, db, monkeypatch):
    def boom(payload, sig):
        raise ValueError("bad sig")

    monkeypatch.setattr(gw, "construct_event", boom)
    res = http_client.post(
        "/subscriptions/stripe/webhook/", data="{}", content_type="application/json"
    )
    assert res.status_code == 400


@_PUBLIC_HOST
def test_webhook_view_valid_processes(http_client, db, monkeypatch):
    m = _member()
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"member_id": str(m.pk)},
                "customer": "cus_9",
                "subscription": "sub_9",
            }
        },
    }
    monkeypatch.setattr(gw, "construct_event", lambda payload, sig: event)
    res = http_client.post(
        "/subscriptions/stripe/webhook/", data="{}", content_type="application/json"
    )
    assert res.status_code == 200
    assert MemberSubscription.objects.get(member=m).stripe_customer_id == "cus_9"
