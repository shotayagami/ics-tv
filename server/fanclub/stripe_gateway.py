# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe API の薄いラッパ (#27 Phase B、FC有料ティア)。

subscriptions.stripe_gateway と同じ思想(カード情報は自前で持たず Stripe ホスト型に委譲、
key 未設定なら StripeNotConfiguredError)。FC はクリエイター単位の Stripe Connect Express
destination charge: プラットフォームが Checkout で徴収し、手数料(application_fee_percent)を
差し引いた残額を connected account へ自動送金する(資金の滞留を作らない収納代行相当の構成)。
Customer/Subscription 自体はプラットフォームの Stripe アカウント上に存在するため、解約は
通常の Customer Portal(視聴者サブスクと同じ経路)で完結する。
"""

from __future__ import annotations

import stripe
from django.conf import settings

from core.utils import stripe_to_dict


class StripeChangeFailedError(RuntimeError):
    """Stripe から必要な情報が取れずティア変更を続行できない (retrieve 結果が想定外)。"""


class StripeNotConfiguredError(RuntimeError):
    pass


def _client():
    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise StripeNotConfiguredError("STRIPE_SECRET_KEY 未設定")
    stripe.api_key = key
    return stripe


def ensure_connect_account(creator) -> str:
    """creator の Stripe Connect Express account を作成/再利用し account_id を返す。"""
    if creator.stripe_connect_account_id:
        return creator.stripe_connect_account_id
    acct = _client().Account.create(
        type="express",
        business_type="individual",
        metadata={"creator_id": str(creator.pk)},
    )
    return acct.id


def create_account_link(*, account_id: str, refresh_url: str, return_url: str) -> str:
    """Connect Express オンボーディング(本人確認・銀行口座登録)への遷移URL。"""
    link = _client().AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url


def ensure_customer(member, membership) -> str:
    """member の Stripe Customer を作成/再利用し customer_id を返す(プラットフォーム側の顧客)。"""
    if membership and membership.stripe_customer_id:
        return membership.stripe_customer_id
    cust = _client().Customer.create(email=member.email, metadata={"member_id": str(member.pk)})
    return cust.id


def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    connect_account_id: str,
    application_fee_percent: float,
    success_url: str,
    cancel_url: str,
    member_id: int,
    creator_id: int,
    tier_id: int,
) -> str:
    """destination charge の Checkout Session を作る。tier_id を metadata に積み、
    checkout.session.completed だけで(price展開なしに)加入先ティアを一意特定できるようにする。
    """
    meta = {"member_id": str(member_id), "creator_id": str(creator_id), "tier_id": str(tier_id)}
    sess = _client().checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=meta,
        subscription_data={
            "application_fee_percent": application_fee_percent,
            "transfer_data": {"destination": connect_account_id},
            "metadata": meta,
        },
    )
    return sess.url


def create_portal_session(*, customer_id: str, return_url: str) -> str:
    sess = _client().billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return sess.url


def _subscription_item_id(sub) -> str:
    items = (sub.get("items") or {}).get("data") or []
    return items[0].get("id") or "" if items else ""


def change_subscription_price_now(*, subscription_id: str, new_price_id: str) -> None:
    """アップグレード: サブスクの Price を即時差し替え、差額を日割りで即時請求する。

    proration_behavior="always_invoice" は日割り分の請求書をその場で発行・確定するため、
    上位ティアの権益を次回請求日まで待たずに開放できる (F5「即時アップ + 日割り」)。
    application_fee_percent / transfer_data はサブスク側の設定なので Price 差し替えでは
    失われない (destination charge の分配構成はそのまま維持される)。
    """
    client = _client()
    sub = stripe_to_dict(client.Subscription.retrieve(subscription_id))
    item_id = _subscription_item_id(sub)
    if not item_id:
        raise StripeChangeFailedError("サブスクの明細が取得できませんでした")
    client.Subscription.modify(
        subscription_id,
        items=[{"id": item_id, "price": new_price_id}],
        proration_behavior="always_invoice",
    )


def schedule_subscription_price_at_period_end(*, subscription_id: str, new_price_id: str) -> int:
    """ダウングレード: 現在の期間は現行 Price を維持し、次期から新 Price へ移す。

    既に支払い済みの期間の権益を取り上げないため、Subscription Schedule の2フェーズ構成
    (現フェーズ=現行 Price をそのまま / 次フェーズ=新 Price を1周期) にする。
    end_behavior="release" で次フェーズ開始後にスケジュールを外し、以降は新 Price の
    通常サブスクとして継続させる。既存スケジュールがあるときは作り直さず差し替える
    (SubscriptionSchedule.create(from_subscription=...) は二重作成を許さないうえ、
    許されたとしても請求が二重化する)。

    戻り値は次期開始の unix 秒 (= 現フェーズの end_date)。UI の「適用予定日」に使う。
    """
    client = _client()
    sub = stripe_to_dict(client.Subscription.retrieve(subscription_id))
    schedule_id = sub.get("schedule") or ""
    if schedule_id:
        sched = stripe_to_dict(client.SubscriptionSchedule.retrieve(schedule_id))
    else:
        sched = stripe_to_dict(
            client.SubscriptionSchedule.create(from_subscription=subscription_id)
        )
    phases = sched.get("phases") or []
    if not phases:
        raise StripeChangeFailedError("スケジュールのフェーズが取得できませんでした")
    cur = phases[0]
    end_date = cur.get("end_date")
    if not end_date:
        raise StripeChangeFailedError("現在の請求期間の終了日が取得できませんでした")
    cur_items = [
        {"price": it.get("price"), "quantity": it.get("quantity") or 1}
        for it in (cur.get("items") or [])
    ]
    client.SubscriptionSchedule.modify(
        sched["id"],
        phases=[
            {
                "items": cur_items,
                "start_date": cur.get("start_date"),
                "end_date": end_date,
                "proration_behavior": "none",
            },
            {
                "items": [{"price": new_price_id, "quantity": 1}],
                "iterations": 1,
                "proration_behavior": "none",
            },
        ],
        end_behavior="release",
    )
    return int(end_date)


def release_subscription_schedule(*, subscription_id: str) -> bool:
    """予約済みのダウングレードを取り消す (スケジュールを外し現行 Price のまま継続させる)。

    release はサブスク自体を解約せず、適用中フェーズの内容を保ったまま切り離す。
    スケジュールが無ければ何もせず False (べき等)。
    """
    client = _client()
    sub = stripe_to_dict(client.Subscription.retrieve(subscription_id))
    schedule_id = sub.get("schedule") or ""
    if not schedule_id:
        return False
    client.SubscriptionSchedule.release(schedule_id)
    return True


def ensure_contract_customer(creator, contract) -> str:
    """枠サブスク(B2B・プラットフォーム直接課金)の Stripe Customer を作成/再利用する。

    FC 会費(destination charge)とは別体系: creator が「支払う側」なので Connect の
    connected account ではなく通常の Customer として扱う(subscriptions.stripe_gateway と
    同じプラットフォーム直接課金の構成)。
    """
    if contract and contract.stripe_customer_id:
        return contract.stripe_customer_id
    cust = _client().Customer.create(
        email=creator.contact_email or "",
        name=creator.name,
        metadata={"creator_id": str(creator.pk)},
    )
    return cust.id


def create_contract_checkout_session(
    *,
    customer_id: str,
    monthly_fee_minor: int,
    title: str,
    success_url: str,
    cancel_url: str,
    contract_id: int,
) -> str:
    """枠サブスク(B2B)の Checkout Session を作る。契約ごとに金額が異なるため、事前作成 Price
    ではなく price_data で都度動的に組む(FC 有料ティアの固定 Price とはこの点のみ異なる)。
    destination charge ではなくプラットフォームの通常売上として計上する(application_fee/
    transfer_data は使わない)。
    """
    meta = {"contract_id": str(contract_id)}
    sess = _client().checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[
            {
                "price_data": {
                    "currency": "jpy",
                    "unit_amount": monthly_fee_minor,
                    "recurring": {"interval": "month"},
                    "product_data": {"name": title},
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=meta,
        subscription_data={"metadata": meta},
    )
    return sess.url


def construct_event(payload: bytes, sig_header: str) -> dict:
    """Webhook 署名を検証し Stripe Event を dict で返す(失敗は例外)。

    stripe>=12 の Event は dict ではなく `.get()` が使えないため、ここで plain dict に
    落としてから webhook ハンドラへ渡す (ハンドラ側は素の dict 前提で書かれている)。
    """
    secret = settings.STRIPE_FC_WEBHOOK_SECRET
    if not secret:
        # subscriptions.stripe_gateway.construct_event と同じ理由 (空鍵の HMAC を受理しない)。
        raise StripeNotConfiguredError("STRIPE_FC_WEBHOOK_SECRET 未設定")
    event = stripe.Webhook.construct_event(payload, sig_header, secret)
    return stripe_to_dict(event)
