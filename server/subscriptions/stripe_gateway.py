# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe API の薄いラッパ (Checkout / Customer Portal / Webhook 署名検証)。

カード情報は自前で持たず Stripe ホスト型に委譲する。key 未設定なら StripeNotConfiguredError。
"""

from __future__ import annotations

import stripe
from django.conf import settings

from core.utils import stripe_to_dict


class StripeNotConfiguredError(RuntimeError):
    pass


def _client():
    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise StripeNotConfiguredError("STRIPE_SECRET_KEY 未設定")
    stripe.api_key = key
    return stripe


def ensure_customer(member, sub) -> str:
    """member の Stripe Customer を作成/再利用し customer_id を返す。"""
    if sub and sub.stripe_customer_id:
        return sub.stripe_customer_id
    cust = _client().Customer.create(email=member.email, metadata={"member_id": str(member.pk)})
    return cust.id


def create_checkout_session(*, customer_id, price_id, success_url, cancel_url, member_id) -> str:
    sess = _client().checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"member_id": str(member_id)},
        subscription_data={"metadata": {"member_id": str(member_id)}},  # webhook で会員を引く
    )
    return sess.url


def create_portal_session(*, customer_id, return_url) -> str:
    sess = _client().billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return sess.url


def construct_event(payload: bytes, sig_header: str) -> dict:
    """Webhook 署名を検証し Stripe Event を dict で返す (失敗は例外)。

    stripe>=12 の Event は dict ではなく `.get()` が使えないため、ここで plain dict に
    落としてから webhook ハンドラへ渡す (ハンドラ側は素の dict 前提で書かれている)。
    """
    event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    return stripe_to_dict(event)
