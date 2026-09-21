# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""在籍より先に届いた invoice.payment_succeeded の保留 (FcPendingInvoice)。

Stripe はイベントの到着順を保証しない。初回の invoice が在籍を作る checkout.session.completed より
先に届いても、精算行 (FcSettlement) を取りこぼさない。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from fanclub import services as fc_services
from fanclub import webhook as wh
from fanclub.models import Creator, CreatorTier, FcPendingInvoice, FcSettlement
from members.models import Member


def _member():
    m = Member(
        email="m@example.com", nickname="t", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    return m


def _creator_and_tier():
    c = Creator.objects.create(
        name="サークルA", slug="circle-a", stripe_connect_account_id="acct_test"
    )
    fc_services.ensure_free_tier(c)
    t = CreatorTier.objects.create(
        creator=c, level=1, name="ベーシック", price_minor=500, stripe_price_id="price_abc"
    )
    return c, t


def _invoice(
    evt_id, invoice_id="in_1", *, sub_id="sub_1", customer="cus_1", amount=500, email=None
):
    obj = {
        "id": invoice_id,
        "subscription": sub_id,
        "customer": customer,
        "amount_paid": amount,
        "currency": "jpy",
        "charge": "ch_1",
        "lines": {"data": [{"period": {"start": 1000, "end": 2000}}]},
    }
    if email:
        obj["customer_email"] = email
    return {
        "id": evt_id,
        "type": "invoice.payment_succeeded",
        "created": 1000,
        "data": {"object": obj},
    }


def _checkout(m, c, t, evt_id="evt_co", *, sub_id="sub_1", customer="cus_1"):
    return {
        "id": evt_id,
        "type": "checkout.session.completed",
        "created": 1001,
        "data": {
            "object": {
                "metadata": {"member_id": str(m.pk), "creator_id": str(c.pk), "tier_id": str(t.pk)},
                "customer": customer,
                "subscription": sub_id,
            }
        },
    }


def test_invoice_before_checkout_is_held_then_recorded_when_membership_is_created(db):
    c, t = _creator_and_tier()
    m = _member()
    wh.handle_event(_invoice("evt_inv"))
    assert FcSettlement.objects.count() == 0
    assert FcPendingInvoice.objects.filter(stripe_invoice_id="in_1").count() == 1

    wh.handle_event(_checkout(m, c, t))
    s = FcSettlement.objects.get(stripe_invoice_id="in_1")
    assert (s.creator_id, s.member_id, s.gross_amount_minor) == (c.pk, m.pk, 500)
    assert s.stripe_charge_id == "ch_1" and s.period_end is not None
    assert not FcPendingInvoice.objects.exists()


def test_held_invoice_is_recorded_when_a_subscription_event_binds_a_free_member(db):
    c, _t = _creator_and_tier()
    m = _member()
    fc_services.join_free_tier(m, c)
    wh.handle_event(_invoice("evt_inv"))
    assert FcPendingInvoice.objects.count() == 1
    wh.handle_event(
        {
            "id": "evt_sub",
            "type": "customer.subscription.created",
            "created": 1001,
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "metadata": {"member_id": str(m.pk), "creator_id": str(c.pk)},
                    "items": {"data": [{"price": {"id": "price_abc"}}]},
                }
            },
        }
    )
    assert FcSettlement.objects.filter(stripe_invoice_id="in_1").count() == 1
    assert not FcPendingInvoice.objects.exists()


def test_resent_invoice_with_another_event_id_is_held_once_and_recorded_once(db):
    c, t = _creator_and_tier()
    m = _member()
    wh.handle_event(_invoice("evt_a"))
    wh.handle_event(_invoice("evt_b"))
    assert FcPendingInvoice.objects.count() == 1
    wh.handle_event(_checkout(m, c, t))
    wh.handle_event(_invoice("evt_c"))
    assert FcSettlement.objects.filter(stripe_invoice_id="in_1").count() == 1
    assert not FcPendingInvoice.objects.exists()


def test_invoice_of_another_subscription_stays_held(db):
    c, t = _creator_and_tier()
    m = _member()
    wh.handle_event(_invoice("evt_other", "in_other", sub_id="sub_other", customer="cus_other"))
    wh.handle_event(_checkout(m, c, t))
    assert FcSettlement.objects.count() == 0
    assert FcPendingInvoice.objects.filter(stripe_invoice_id="in_other").exists()


def test_zero_amount_invoice_is_not_held(db):
    wh.handle_event(_invoice("evt_zero", amount=0))
    assert not FcPendingInvoice.objects.exists()


def test_held_invoice_keeps_no_personal_data(db):
    wh.handle_event(_invoice("evt_pii", email="person@example.invalid"))
    held = FcPendingInvoice.objects.get()
    assert "person@example.invalid" not in str(held.payload)
    assert set(held.payload) == {
        "id",
        "subscription",
        "customer",
        "amount_paid",
        "currency",
        "charge",
        "lines",
    }


def test_stale_held_invoices_are_dropped(db):
    wh.handle_event(_invoice("evt_old", "in_old", sub_id="sub_old", customer="cus_old"))
    FcPendingInvoice.objects.update(created_at=timezone.now() - timedelta(days=31))
    wh.handle_event(_invoice("evt_new", "in_new", sub_id="sub_new", customer="cus_new"))
    assert list(FcPendingInvoice.objects.values_list("stripe_invoice_id", flat=True)) == ["in_new"]
