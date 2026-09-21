# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe Webhook 2 系統 (視聴者サブスク / ファンクラブ) の入口と分離。

- 入口: 実際の署名検証を通す (既存の view 試験は construct_event を差し替えている)。
  秘密が空のとき、空鍵で署名した偽イベントを受理しない。
- 分離: 両系統は同じ Stripe アカウントを共用し、同種のイベントが両エンドポイントへ届きうる。
  ファンクラブの購読・Checkout が視聴者サブスクの会員権を付与しない。
- 順序: 解約を適用済みの在籍・契約を、遅れて届いた checkout が ACTIVE へ戻さない。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from django.test import override_settings

from fanclub import services as fc_services
from fanclub import webhook as fc_wh
from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorTier,
    FcProcessedStripeEvent,
    MembershipStatus,
    SlotContract,
    SlotContractStatus,
)
from members.models import Member
from subscriptions import webhook as sub_wh
from subscriptions.models import MemberSubscription, ProcessedStripeEvent
from subscriptions.services import has_active_subscription

VIEWER_URL = "/subscriptions/stripe/webhook/"
FC_URL = "/fanclub/stripe/webhook/"
# (url, 秘密の設定名, 台帳モデル)
ENDPOINTS = [
    pytest.param(VIEWER_URL, "STRIPE_WEBHOOK_SECRET", ProcessedStripeEvent, id="viewer"),
    pytest.param(FC_URL, "STRIPE_FC_WEBHOOK_SECRET", FcProcessedStripeEvent, id="fanclub"),
]
_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_SECRET_A = "whsec_test_a"  # pragma: allowlist secret - test only
_SECRET_B = "whsec_test_b"  # pragma: allowlist secret - test only


def _sign(payload: bytes, secret: str, *, at: int | None = None) -> str:
    t = int(time.time()) if at is None else at
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _body(evt_id="evt_iso_1") -> bytes:
    return json.dumps(
        {
            "id": evt_id,
            "object": "event",
            "type": "invoice.created",
            "created": int(time.time()),
            "data": {"object": {}},
        }
    ).encode()


def _post(client, url, body, header):
    return client.post(
        url, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=header
    )


# ---- 入口 (署名検証) ----


@_PUBLIC_HOST
@pytest.mark.parametrize(("url", "setting", "ledger"), ENDPOINTS)
def test_empty_secret_is_503_even_for_a_forged_signature(
    db, http_client, settings, url, setting, ledger
):
    setattr(settings, setting, "")
    body = _body()
    res = _post(http_client, url, body, _sign(body, ""))
    assert res.status_code == 503
    assert ledger.objects.count() == 0


@_PUBLIC_HOST
@pytest.mark.parametrize(("url", "setting", "ledger"), ENDPOINTS)
def test_valid_signature_is_accepted_once(db, http_client, settings, url, setting, ledger):
    setattr(settings, setting, _SECRET_A)
    body = _body()
    for _ in range(2):
        assert _post(http_client, url, body, _sign(body, _SECRET_A)).status_code == 200
    assert ledger.objects.count() == 1


@_PUBLIC_HOST
@pytest.mark.parametrize(("url", "setting", "ledger"), ENDPOINTS)
def test_wrong_secret_and_stale_timestamp_and_tampered_body_are_rejected(
    db, http_client, settings, url, setting, ledger
):
    setattr(settings, setting, _SECRET_A)
    body = _body()
    assert _post(http_client, url, body, _sign(body, _SECRET_B)).status_code == 400
    assert (
        _post(http_client, url, body, _sign(body, _SECRET_A, at=int(time.time()) - 900)).status_code
        == 400
    )
    assert _post(http_client, url, body + b" ", _sign(body, _SECRET_A)).status_code == 400
    assert _post(http_client, url, body, "").status_code == 400
    assert ledger.objects.count() == 0


# ---- 分離 (どちらの系統がどのオブジェクトを扱うか) ----


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    return m


def _creator_with_paid_tier():
    c = Creator.objects.create(name="サークルA", slug="circle-a")
    fc_services.ensure_free_tier(c)
    t = CreatorTier.objects.create(
        creator=c, level=1, name="ベーシック", price_minor=500, stripe_price_id="price_fc"
    )
    return c, t


def _fc_meta(member, creator, tier):
    return {"member_id": str(member.pk), "creator_id": str(creator.pk), "tier_id": str(tier.pk)}


def _sub_event(*, evt_id, etype, created, meta, status="active", sub_id="sub_1", customer="cus_1"):
    return {
        "id": evt_id,
        "type": etype,
        "created": created,
        "data": {
            "object": {
                "id": sub_id,
                "customer": customer,
                "status": status,
                "cancel_at_period_end": False,
                "current_period_end": int(time.time()) + 30 * 86400,
                "metadata": meta,
                "items": {"data": [{"price": {"id": "price_fc"}}]},
            }
        },
    }


def _checkout_event(*, evt_id, created, meta, sub_id="sub_1", customer="cus_1"):
    return {
        "id": evt_id,
        "type": "checkout.session.completed",
        "created": created,
        "data": {"object": {"metadata": meta, "customer": customer, "subscription": sub_id}},
    }


def test_viewer_handler_ignores_fanclub_checkout_and_subscription(db):
    c, t = _creator_with_paid_tier()
    m = _member()
    meta = _fc_meta(m, c, t)
    sub_wh.handle_event(_checkout_event(evt_id="evt_co", created=1000, meta=meta))
    sub_wh.handle_event(
        _sub_event(evt_id="evt_s1", etype="customer.subscription.created", created=1001, meta=meta)
    )
    sub_wh.handle_event(
        _sub_event(evt_id="evt_s2", etype="customer.subscription.updated", created=1002, meta=meta)
    )
    assert not MemberSubscription.objects.filter(member=m).exists()
    assert not has_active_subscription(m)


def test_viewer_handler_ignores_slot_contract_events(db):
    m = _member()
    meta = {"member_id": str(m.pk), "contract_id": "1"}
    sub_wh.handle_event(
        _sub_event(evt_id="evt_c1", etype="customer.subscription.created", created=1001, meta=meta)
    )
    assert not MemberSubscription.objects.filter(member=m).exists()


def test_viewer_handler_still_applies_its_own_subscription(db):
    m = _member()
    meta = {"member_id": str(m.pk)}
    sub_wh.handle_event(
        _sub_event(
            evt_id="evt_v1",
            etype="customer.subscription.created",
            created=1001,
            meta=meta,
            sub_id="sub_v",
        )
    )
    assert MemberSubscription.objects.get(member=m).stripe_subscription_id == "sub_v"
    assert has_active_subscription(m)


def test_fanclub_handler_ignores_viewer_subscription_events(db):
    c, t = _creator_with_paid_tier()
    m = _member()
    membership = CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        stripe_customer_id="cus_fc",
        stripe_subscription_id="sub_fc",
    )
    fc_wh.handle_event(
        _sub_event(
            evt_id="evt_vx",
            etype="customer.subscription.deleted",
            created=2000,
            meta={"member_id": str(m.pk)},
            status="canceled",
            sub_id="sub_viewer",
            customer="cus_viewer",
        )
    )
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.stripe_subscription_id == "sub_fc"


# ---- 順序 (解約の後に遅れて届いた checkout) ----


def test_late_checkout_does_not_reactivate_a_membership_ended_by_a_newer_event(db):
    c, t = _creator_with_paid_tier()
    m = _member()
    meta = _fc_meta(m, c, t)
    fc_services.join_free_tier(m, c)
    fc_wh.handle_event(
        _sub_event(
            evt_id="evt_del",
            etype="customer.subscription.deleted",
            created=2000,
            meta=meta,
            status="canceled",
        )
    )
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == MembershipStatus.LEFT
    fc_wh.handle_event(_checkout_event(evt_id="evt_late", created=1500, meta=meta))
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.LEFT
    # 対照: 解約より新しい checkout (再加入) は在籍に戻す
    fc_wh.handle_event(_checkout_event(evt_id="evt_new", created=2500, meta=meta, sub_id="sub_2"))
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.stripe_subscription_id == "sub_2"


def test_late_contract_checkout_does_not_reactivate_an_ended_contract(db):
    c, _t = _creator_with_paid_tier()
    contract = SlotContract.objects.create(
        creator=c, title="レギュラー枠", monthly_fee_minor=100_000, starts_on="2026-08-01"
    )
    meta = {"contract_id": str(contract.pk)}
    fc_wh.handle_event(_checkout_event(evt_id="evt_k1", created=1000, meta=meta))
    fc_wh.handle_event(
        _sub_event(
            evt_id="evt_k2",
            etype="customer.subscription.deleted",
            created=2000,
            meta=meta,
            status="canceled",
        )
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ENDED
    fc_wh.handle_event(_checkout_event(evt_id="evt_k3", created=1500, meta=meta))
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ENDED
