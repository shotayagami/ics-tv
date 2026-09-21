# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-3: checkout の二重サブスク (二重課金) 防止。

有効サブスクを持つ会員が再度 checkout しても新しい Stripe Checkout を作らず、管理ポータルへ
誘導する (302 /subscriptions/)。有効サブスクが無ければ従来どおり Checkout 作成を試みる
(Stripe 未設定のテスト環境では 503 になる = ガードで止まっていない証拠)。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Member
from subscriptions.models import MemberSubscription, Plan, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


@_PUBLIC_HOST
def test_checkout_blocked_when_already_active(http_client, db):
    m = _member()
    plan = Plan.objects.create(
        name="松", slug="matsu", amount=980, rank=3, stripe_price_id="price_x"
    )
    MemberSubscription.objects.create(
        member=m,
        plan=plan,
        status=SubStatus.ACTIVE,
        stripe_customer_id="cus_x",
        current_period_end=timezone.now() + timedelta(days=30),
    )
    _login(http_client)
    res = http_client.post("/subscriptions/checkout/matsu/")
    # 有効サブスクありなら 302 で管理へ (Stripe を叩かない=503 にならない)
    assert res.status_code == 302 and res.url == "/subscriptions/"


@_PUBLIC_HOST
def test_checkout_proceeds_without_active_sub(http_client, db):
    _member()
    Plan.objects.create(name="梅", slug="ume", amount=480, rank=1, stripe_price_id="price_x")
    _login(http_client)
    res = http_client.post("/subscriptions/checkout/ume/")
    # 有効サブスク無し → ガードは通過し Checkout 作成を試みる。Stripe 未設定ゆえ 503。
    assert res.status_code == 503


@_PUBLIC_HOST
def test_checkout_proceeds_when_sub_inactive(http_client, db):
    """解約済 (canceled) は is_active=False なので再購入できる。"""
    m = _member()
    plan = Plan.objects.create(
        name="竹", slug="take", amount=680, rank=2, stripe_price_id="price_x"
    )
    MemberSubscription.objects.create(member=m, plan=plan, status=SubStatus.CANCELED)
    _login(http_client)
    res = http_client.post("/subscriptions/checkout/take/")
    assert res.status_code == 503  # ガードは通過 (Stripe 未設定で 503)


# ---- 特商法 申込み最終確認画面 (§5.2) ----


@_PUBLIC_HOST
@override_settings(STRIPE_SECRET_KEY="sk_test_x")  # pragma: allowlist secret - test only
def test_confirm_renders_when_purchasable(http_client, db):
    _member()
    Plan.objects.create(name="梅", slug="ume", amount=480, rank=1, stripe_price_id="price_x")
    _login(http_client)
    res = http_client.get("/subscriptions/checkout/ume/confirm/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "¥480" in body and "同意して申し込む" in body
    assert 'action="/subscriptions/checkout/ume/"' in body


@_PUBLIC_HOST
def test_confirm_redirects_when_stripe_unconfigured(http_client, db):
    _member()
    Plan.objects.create(name="梅", slug="ume", amount=480, rank=1, stripe_price_id="price_x")
    _login(http_client)
    res = http_client.get("/subscriptions/checkout/ume/confirm/")
    assert res.status_code == 302 and res.url == "/subscriptions/"


@_PUBLIC_HOST
@override_settings(STRIPE_SECRET_KEY="sk_test_x")  # pragma: allowlist secret - test only
def test_confirm_redirects_when_already_active(http_client, db):
    m = _member()
    plan = Plan.objects.create(
        name="松", slug="matsu", amount=980, rank=3, stripe_price_id="price_x"
    )
    MemberSubscription.objects.create(
        member=m,
        plan=plan,
        status=SubStatus.ACTIVE,
        stripe_customer_id="cus_x",
        current_period_end=timezone.now() + timedelta(days=30),
    )
    _login(http_client)
    res = http_client.get("/subscriptions/checkout/matsu/confirm/")
    assert res.status_code == 302 and res.url == "/subscriptions/"
