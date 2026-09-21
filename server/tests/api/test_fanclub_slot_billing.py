# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""枠サブスク(SlotContract)の Stripe Billing 化 (#27 Part 2)。

プラットフォーム直接課金(Connect 不使用、契約ごとに price_data で動的に金額を組む)。
実 API は monkeypatch する (test_fanclub_stripe.py と同じ思想)。
"""

from __future__ import annotations

from django.test import Client, override_settings
from django.utils import timezone

from fanclub import stripe_gateway as gw
from fanclub import webhook as wh
from fanclub.models import Creator, CreatorAccount, SlotContract, SlotContractStatus

CREATOR_HOST = "creator.example.com"
_HOSTS = override_settings(
    ALLOWED_HOSTS=["testserver", CREATOR_HOST],
    ICSTV_ADMIN_HOSTS=[],
    ICSTV_CREATOR_HOSTS=[CREATOR_HOST],
)


def _creator(slug="circle-a"):
    return Creator.objects.create(name="サークルA", slug=slug)


def _account(creator, email="me@example.com"):
    return CreatorAccount.objects.create(creator=creator, email=email, google_sub="sub-1")


def _login_as(web, account):
    session = web.session
    session["creator_account_id"] = account.pk
    session.save()


def _contract(creator, *, status=SlotContractStatus.DRAFT, **kw):
    return SlotContract.objects.create(
        creator=creator,
        title="レギュラー枠",
        monthly_fee_minor=100_000,
        starts_on="2026-08-01",
        status=status,
        **kw,
    )


def _epoch_future(days=30):
    return int(timezone.now().timestamp()) + days * 86400


# ---- Checkout (creator.* セルフサービス) ----


@_HOSTS
def test_contract_checkout_creates_session(db, monkeypatch):
    web = Client()
    c = _creator()
    contract = _contract(c)
    account = _account(c)
    _login_as(web, account)
    monkeypatch.setattr(gw, "ensure_contract_customer", lambda creator, contract: "cus_test")
    monkeypatch.setattr(
        gw, "create_contract_checkout_session", lambda **kw: "https://checkout.stripe/test"
    )
    res = web.post(f"/contracts/{contract.id}/checkout/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url == "https://checkout.stripe/test"


@_HOSTS
def test_contract_checkout_rejects_non_draft(db):
    web = Client()
    c = _creator()
    contract = _contract(c, status=SlotContractStatus.ENDED)
    account = _account(c)
    _login_as(web, account)
    res = web.post(f"/contracts/{contract.id}/checkout/", HTTP_HOST=CREATOR_HOST, follow=True)
    msgs = [str(m) for m in res.context["messages"]]
    assert any("対象ではありません" in m for m in msgs)


@_HOSTS
def test_contract_checkout_rejects_already_billed(db):
    web = Client()
    c = _creator()
    contract = _contract(c, stripe_subscription_id="sub_existing")
    account = _account(c)
    _login_as(web, account)
    res = web.post(f"/contracts/{contract.id}/checkout/", HTTP_HOST=CREATOR_HOST, follow=True)
    msgs = [str(m) for m in res.context["messages"]]
    assert any("既に" in m for m in msgs)


@_HOSTS
def test_contract_checkout_503_when_stripe_unconfigured(db):
    web = Client()
    c = _creator()
    contract = _contract(c)
    account = _account(c)
    _login_as(web, account)
    res = web.post(f"/contracts/{contract.id}/checkout/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 503


@_HOSTS
def test_contract_checkout_other_creator_contract_404(db):
    web = Client()
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    other_contract = _contract(c2)
    account = _account(c1)
    _login_as(web, account)
    res = web.post(f"/contracts/{other_contract.id}/checkout/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 404


@_HOSTS
def test_contract_checkout_requires_login(db):
    web = Client()
    c = _creator()
    contract = _contract(c)
    res = web.post(f"/contracts/{contract.id}/checkout/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url.startswith("/login/")


# ---- Portal ----


@_HOSTS
def test_contract_portal_noop_without_customer(db):
    web = Client()
    c = _creator()
    contract = _contract(c)
    account = _account(c)
    _login_as(web, account)
    res = web.post(f"/contracts/{contract.id}/portal/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url == "/contracts/"


@_HOSTS
def test_contract_portal_creates_session(db, monkeypatch):
    web = Client()
    c = _creator()
    contract = _contract(
        c,
        status=SlotContractStatus.ACTIVE,
        stripe_customer_id="cus_x",
        stripe_subscription_id="sub_x",
    )
    account = _account(c)
    _login_as(web, account)
    monkeypatch.setattr(gw, "create_portal_session", lambda **kw: "https://portal.stripe/test")
    res = web.post(f"/contracts/{contract.id}/portal/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url == "https://portal.stripe/test"


# ---- Webhook ----


def _checkout_event(contract, *, customer="cus_1", subscription="sub_1"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"contract_id": str(contract.pk)},
                "customer": customer,
                "subscription": subscription,
            }
        },
    }


def _sub_event(contract, *, evt_id, status, created, etype, customer="cus_1", metadata=True):
    obj = {
        "id": "sub_1",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": False,
        "current_period_end": _epoch_future(),
    }
    if metadata:
        obj["metadata"] = {"contract_id": str(contract.pk)}
    return {"id": evt_id, "created": created, "type": etype, "data": {"object": obj}}


def test_webhook_checkout_completed_activates_contract(db):
    c = _creator()
    contract = _contract(c)
    wh.handle_event(_checkout_event(contract))
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ACTIVE
    assert contract.stripe_customer_id == "cus_1"
    assert contract.stripe_subscription_id == "sub_1"


def test_webhook_subscription_updated_syncs_period(db):
    c = _creator()
    contract = _contract(c)
    wh.handle_event(_checkout_event(contract))
    wh.handle_event(
        _sub_event(
            contract,
            evt_id="evt_1",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ACTIVE
    assert contract.current_period_end is not None


def test_webhook_subscription_deleted_marks_ended(db):
    c = _creator()
    contract = _contract(c)
    wh.handle_event(_checkout_event(contract))
    wh.handle_event(
        _sub_event(
            contract,
            evt_id="evt_del",
            status="canceled",
            created=2000,
            etype="customer.subscription.deleted",
        )
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ENDED


def test_webhook_subscription_resolves_by_subscription_id_without_metadata(db):
    """checkout.session.completed 未着でも stripe_subscription_id で解決できる場合は同期する。"""
    c = _creator()
    contract = _contract(c, stripe_subscription_id="sub_1")
    wh.handle_event(
        _sub_event(
            contract,
            evt_id="evt_1",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
            metadata=False,
        )
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ACTIVE


def test_webhook_drops_stale_out_of_order_event(db):
    c = _creator()
    contract = _contract(c)
    wh.handle_event(_checkout_event(contract))
    wh.handle_event(
        _sub_event(
            contract,
            evt_id="evt_new",
            status="canceled",
            created=5000,
            etype="customer.subscription.deleted",
        )
    )
    wh.handle_event(
        _sub_event(
            contract,
            evt_id="evt_old",
            status="active",
            created=1000,
            etype="customer.subscription.updated",
        )
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.ENDED  # 古い active イベントは無視される


def test_webhook_does_not_confuse_contract_and_membership_checkout(db):
    """contract_id 無し(FC 有料ティア加入)の checkout event は SlotContract に影響しない。"""
    from fanclub import services as fc_services
    from fanclub.models import CreatorTier
    from members.models import Member

    c = _creator()
    contract = _contract(c)
    tier = CreatorTier.objects.create(creator=c, level=1, name="B", price_minor=500)
    fc_services.ensure_free_tier(c)
    m = Member(
        email="m@example.com", nickname="t", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    wh.handle_event(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "member_id": str(m.pk),
                        "creator_id": str(c.pk),
                        "tier_id": str(tier.pk),
                    },
                    "customer": "cus_member",
                    "subscription": "sub_member",
                }
            },
        }
    )
    contract.refresh_from_db()
    assert contract.status == SlotContractStatus.DRAFT  # 無関係
    assert contract.stripe_subscription_id == ""
