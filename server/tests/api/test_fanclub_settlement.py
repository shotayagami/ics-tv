# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FC分配元帳 (#27 Phase B): invoice.payment_succeeded/dispute webhook + studio/creator側レポート。"""

from __future__ import annotations

from django.db.models import Sum
from django.test import Client, override_settings

from fanclub import services as fc_services
from fanclub import webhook as wh
from fanclub.models import (
    Creator,
    CreatorAccount,
    CreatorMembership,
    CreatorTier,
    FcSettlement,
    MembershipStatus,
)
from members.models import Member

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_CREATOR_HOST = "creator.example.com"
_CREATOR_HOSTS = override_settings(
    ALLOWED_HOSTS=["testserver", _CREATOR_HOST],
    ICSTV_ADMIN_HOSTS=[],
    ICSTV_CREATOR_HOSTS=[_CREATOR_HOST],
)


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


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


def _membership(member, creator, tier, *, sub_id="sub_1", customer_id="cus_1"):
    return CreatorMembership.objects.create(
        member=member,
        creator=creator,
        tier=tier,
        status=MembershipStatus.ACTIVE,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
    )


def _invoice_event(
    *,
    evt_id,
    invoice_id,
    sub_id="sub_1",
    customer_id="cus_1",
    amount_paid=500,
    charge_id="ch_1",
    currency="jpy",
):
    return {
        "id": evt_id,
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": invoice_id,
                "subscription": sub_id,
                "customer": customer_id,
                "amount_paid": amount_paid,
                "currency": currency,
                "charge": charge_id,
                "lines": {"data": [{"period": {"start": 1000, "end": 2000}}]},
            }
        },
    }


# ---- invoice.payment_succeeded → FcSettlement 記録 ----


def test_webhook_records_settlement_with_fee_split(db, settings):
    settings.STRIPE_CONNECT_APPLICATION_FEE_PERCENT = 10.0
    c = _creator()
    t = _paid_tier(c, price_minor=1000)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", amount_paid=1000))
    s = FcSettlement.objects.get(stripe_invoice_id="in_1")
    assert s.creator_id == c.id
    assert s.member_id == m.id
    assert s.tier_id == t.id
    assert s.gross_amount_minor == 1000
    assert s.application_fee_minor == 100
    assert s.net_amount_minor == 900
    assert s.period_start is not None and s.period_end is not None
    assert s.disputed is False


def test_webhook_settlement_idempotent_by_invoice_id(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1"))
    # 同一 invoice_id を別の event.id で再送されても二重記録しない (念のための保険)
    wh.handle_event(_invoice_event(evt_id="evt_2", invoice_id="in_1"))
    assert FcSettlement.objects.filter(stripe_invoice_id="in_1").count() == 1


def test_webhook_settlement_ignored_when_no_matching_membership(db):
    wh.handle_event(
        _invoice_event(
            evt_id="evt_1", invoice_id="in_orphan", sub_id="sub_none", customer_id="cus_none"
        )
    )
    assert not FcSettlement.objects.exists()


def test_webhook_settlement_ignored_for_zero_amount(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", amount_paid=0))
    assert not FcSettlement.objects.exists()


def test_webhook_settlement_resolves_membership_by_customer_id_fallback(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t, sub_id="sub_other", customer_id="cus_1")
    wh.handle_event(
        _invoice_event(
            evt_id="evt_1", invoice_id="in_1", sub_id="sub_not_matching", customer_id="cus_1"
        )
    )
    assert FcSettlement.objects.filter(stripe_invoice_id="in_1").exists()


# ---- チャージバック追跡 ----


def _dispute_event(evt_id, etype, charge_id="ch_1", status="warning_needs_response"):
    return {
        "id": evt_id,
        "type": etype,
        "data": {"object": {"charge": charge_id, "status": status}},
    }


def test_webhook_dispute_created_marks_disputed(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", charge_id="ch_1"))
    wh.handle_event(_dispute_event("evt_dispute", "charge.dispute.created", charge_id="ch_1"))
    s = FcSettlement.objects.get(stripe_invoice_id="in_1")
    assert s.disputed is True


def test_webhook_dispute_closed_won_clears_disputed(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", charge_id="ch_1"))
    wh.handle_event(_dispute_event("evt_d1", "charge.dispute.created", charge_id="ch_1"))
    wh.handle_event(
        _dispute_event("evt_d2", "charge.dispute.closed", charge_id="ch_1", status="won")
    )
    s = FcSettlement.objects.get(stripe_invoice_id="in_1")
    assert s.disputed is False


def test_webhook_dispute_closed_lost_keeps_disputed(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", charge_id="ch_1"))
    wh.handle_event(_dispute_event("evt_d1", "charge.dispute.created", charge_id="ch_1"))
    wh.handle_event(
        _dispute_event("evt_d2", "charge.dispute.closed", charge_id="ch_1", status="lost")
    )
    s = FcSettlement.objects.get(stripe_invoice_id="in_1")
    assert s.disputed is True


def test_webhook_dispute_unknown_charge_is_noop(db):
    wh.handle_event(_dispute_event("evt_1", "charge.dispute.created", charge_id="ch_unknown"))
    assert not FcSettlement.objects.exists()


# ---- studio API ----

_SETTLEMENTS_BASE = "/api/v1/admin/fanclub/creators"


def test_studio_settlements_summary(staff_client, db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(
        _invoice_event(evt_id="evt_1", invoice_id="in_1", amount_paid=1000, charge_id="ch_1")
    )
    wh.handle_event(
        _invoice_event(evt_id="evt_2", invoice_id="in_2", amount_paid=1000, charge_id="ch_2")
    )
    wh.handle_event(_dispute_event("evt_d", "charge.dispute.created", charge_id="ch_2"))
    data = staff_client.get(f"{_SETTLEMENTS_BASE}/{c.id}/settlements").json()
    assert data["total_gross_jpy"] == 2000
    assert data["disputed_count"] == 1
    assert len(data["items"]) == 2
    assert data["items"][0]["member_email"] == "m@example.com"
    # 明細は通貨とセットで返す (studio 側で ¥ を決め打ちさせない)
    assert data["items"][0]["currency"] == "jpy"
    assert data["totals_by_currency"] == [
        {"currency": "jpy", "total_gross": 2000, "total_fee": 200, "total_net": 1800}
    ]


def test_studio_settlements_requires_staff(http_client, db):
    c = _creator()
    res = http_client.get(f"{_SETTLEMENTS_BASE}/{c.id}/settlements")
    assert res.status_code == 401


# ---- creator.* セルフサービス ----


def _creator_account(creator, email="me@example.com"):
    return CreatorAccount.objects.create(creator=creator, email=email, google_sub="sub-1")


def _login_creator(web, account):
    session = web.session
    session["creator_account_id"] = account.pk
    session.save()


@_CREATOR_HOSTS
def test_creator_settlements_requires_login(db):
    web = Client()
    res = web.get("/settlements/", HTTP_HOST=_CREATOR_HOST)
    assert res.status_code == 302 and res.url.startswith("/login/")


@_CREATOR_HOSTS
def test_creator_settlements_shows_own_totals(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(_invoice_event(evt_id="evt_1", invoice_id="in_1", amount_paid=1000))
    web = Client()
    _login_creator(web, _creator_account(c))
    res = web.get("/settlements/", HTTP_HOST=_CREATOR_HOST)
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "1000" in body


@_CREATOR_HOSTS
def test_creator_settlements_isolated_per_creator(db):
    c1 = _creator(slug="circle-a")
    c2 = _creator(slug="circle-b")
    t1 = _paid_tier(c1, price_id="price_1")
    t2 = _paid_tier(c2, price_id="price_2")
    m1 = _member("m1@example.com")
    m2 = _member("m2@example.com")
    _membership(m1, c1, t1, sub_id="sub_a", customer_id="cus_a")
    _membership(m2, c2, t2, sub_id="sub_b", customer_id="cus_b")
    wh.handle_event(
        _invoice_event(evt_id="evt_1", invoice_id="in_a", sub_id="sub_a", amount_paid=100)
    )
    wh.handle_event(
        _invoice_event(evt_id="evt_2", invoice_id="in_b", sub_id="sub_b", amount_paid=999)
    )
    web = Client()
    _login_creator(web, _creator_account(c1))
    res = web.get("/settlements/", HTTP_HOST=_CREATOR_HOST)
    body = res.content.decode("utf-8")
    assert "100" in body
    assert "999" not in body


# ---- 通貨のガード (#sec L-4/L-5「通貨をサーバ側検証」) ----


def test_webhook_rejects_non_jpy_currency(db):
    """外貨建ての invoice は台帳に載せない。

    サブスクの Price は Stripe ダッシュボード側で通貨が決まるため、誤って外貨建ての
    Price を紐付けると amount_paid がそのまま「円」として記録され、実際の入金と
    2 桁ずれる。本システムは JPY 単一設計なので換算せず fail-closed で拒否する。
    """
    c = _creator()
    t = _paid_tier(c, price_minor=1000)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(
        _invoice_event(evt_id="evt_usd", invoice_id="in_usd", amount_paid=1000, currency="usd")
    )
    assert not FcSettlement.objects.filter(stripe_invoice_id="in_usd").exists()


def test_webhook_accepts_jpy_currency(db):
    c = _creator()
    t = _paid_tier(c, price_minor=1000)
    m = _member()
    _membership(m, c, t)
    wh.handle_event(
        _invoice_event(evt_id="evt_jpy", invoice_id="in_jpy", amount_paid=1000, currency="jpy")
    )
    assert FcSettlement.objects.filter(stripe_invoice_id="in_jpy").exists()


def test_webhook_accepts_missing_currency_field(db):
    """currency が無い形の invoice は従来どおり通す (後方互換)。

    ガードは「明示的に jpy 以外」だけを弾く。Stripe の応答形状が変わって currency が
    落ちたときに、正常な入金まで拒否して台帳が欠けるほうが害が大きい。
    """
    c = _creator()
    t = _paid_tier(c, price_minor=1000)
    m = _member()
    _membership(m, c, t)
    ev = _invoice_event(evt_id="evt_nc", invoice_id="in_nc", amount_paid=1000)
    del ev["data"]["object"]["currency"]
    wh.handle_event(ev)
    assert FcSettlement.objects.filter(stripe_invoice_id="in_nc").exists()


# ---- 通貨別の集計 (通貨をまたいで合算しない) ----


def test_settlement_totals_are_grouped_by_currency(db, client):
    """合計は通貨ごとに分かれる。

    通貨をまたいで足すと「円とドルを足した数字」になり、**一見それらしい値が出るぶん
    誤りに気付けない**。現状は jpy 単一なので実質 1 行だが、混ざった時点で自動的に分かれる。
    """
    c = _creator()
    t = _paid_tier(c, price_minor=1000)
    m = _member()
    FcSettlement.objects.create(
        creator=c,
        member=m,
        tier=t,
        stripe_invoice_id="in_a",
        gross_amount_minor=1000,
        application_fee_minor=100,
        net_amount_minor=900,
        currency="jpy",
    )
    FcSettlement.objects.create(
        creator=c,
        member=m,
        tier=t,
        stripe_invoice_id="in_b",
        gross_amount_minor=2000,
        application_fee_minor=200,
        net_amount_minor=1800,
        currency="usd",
    )
    rows = list(
        c.settlements.values("currency").annotate(g=Sum("gross_amount_minor")).order_by("currency")
    )
    assert rows == [
        {"currency": "jpy", "g": 1000},
        {"currency": "usd", "g": 2000},
    ]
