# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe Webhook の同期ロジック (純関数・event dict 受け)。

MemberSubscription の状態は Stripe が真実。event を受けて upsert する。冪等。
view 側は署名検証のみ行い handle_event(event) を呼ぶ。**event は plain dict であること**
(stripe>=12 の StripeObject は dict を継承しておらず `.get()` が使えない。dict 化は
stripe_gateway.construct_event が担う)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.db import transaction

from members.models import Member
from subscriptions.models import (
    MemberSubscription,
    Plan,
    ProcessedStripeEvent,
    SubStatus,
)

logger = logging.getLogger(__name__)

# ファンクラブ (fanclub.webhook) の Checkout/購読は creator_id / contract_id を metadata に積む。
# 両系統は同じ Stripe アカウントを共用し、同種のイベントが両エンドポイントへ届きうるので、
# この印を持つオブジェクトは視聴者サブスクの対象外として扱う (会員権の誤付与を防ぐ)。
_FANCLUB_METADATA_KEYS = ("creator_id", "contract_id")

# Stripe subscription.status → 自前 SubStatus
_STATUS_MAP = {
    "active": SubStatus.ACTIVE,
    "trialing": SubStatus.TRIALING,
    "past_due": SubStatus.PAST_DUE,
    "canceled": SubStatus.CANCELED,
    "incomplete": SubStatus.INCOMPLETE,
    "incomplete_expired": SubStatus.CANCELED,
    "unpaid": SubStatus.UNPAID,
    "paused": SubStatus.PAST_DUE,
}


def _ts(epoch):
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch else None


def _is_fanclub_owned(meta) -> bool:
    return any((meta or {}).get(k) for k in _FANCLUB_METADATA_KEYS)


def _member_from_metadata(meta) -> Member | None:
    mid = (meta or {}).get("member_id")
    return Member.objects.filter(pk=mid).first() if mid else None


def handle_event(event) -> None:
    """署名検証済み event を処理する。event.id で冪等化 (#sec L-3)。

    記録 (ProcessedStripeEvent) と同期を 1 トランザクションに包む。ハンドラが例外を投げると
    記録ごとロールバックされ、Stripe の再送で再試行できる (= 失敗イベントを取りこぼさない)。
    既処理の event.id は何もせず返す (at-least-once の重複を吸収)。
    """
    event_id = event.get("id") or ""
    if not event_id:
        _dispatch(event)  # id 無し (通常あり得ない) は冪等化できないので従来どおり処理
        return
    with transaction.atomic():
        _, created = ProcessedStripeEvent.objects.get_or_create(
            event_id=event_id, defaults={"event_type": event.get("type", "")}
        )
        if not created:
            return  # 既処理 = 冪等スキップ
        _dispatch(event)


def _dispatch(event) -> None:
    etype = event.get("type", "")
    created = int(event.get("created") or 0)
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed":
        _sync_from_checkout(obj)
    elif etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _sync_from_subscription(obj, event_created=created)
    # invoice.payment_failed は Stripe が subscription.updated(past_due) も送るため追加処理不要


def _sync_from_checkout(session) -> None:
    if _is_fanclub_owned(session.get("metadata")):
        return
    member = _member_from_metadata(session.get("metadata"))
    if member is None:
        return
    MemberSubscription.objects.update_or_create(
        member=member,
        defaults={
            "stripe_customer_id": session.get("customer") or "",
            "stripe_subscription_id": session.get("subscription") or "",
        },
    )


def _sync_from_subscription(sub, event_created: int = 0) -> None:
    if _is_fanclub_owned(sub.get("metadata")):
        return
    sub_id = sub.get("id") or ""
    rec = None
    member = _member_from_metadata(sub.get("metadata"))
    if member is not None:
        rec, _ = MemberSubscription.objects.get_or_create(member=member)
    if rec is None and sub_id:
        rec = MemberSubscription.objects.filter(stripe_subscription_id=sub_id).first()
    if rec is None and sub.get("customer"):
        rec = MemberSubscription.objects.filter(stripe_customer_id=sub.get("customer")).first()
    if rec is None:
        # 紐付け不能 (metadata 無し & 既存無し)。台帳には処理済みと残るので、後から追えるよう記録する。
        logger.info("subscription webhook: unlinked subscription=%s", sub_id)
        return

    # 順序ガード (#sec L-3): 直近適用より古い event は捨てる。deleted(canceled) の後に遅延した
    # updated(active) が後着して特典が復活する/正規会員が締め出される事故を防ぐ。
    if event_created and rec.last_event_created and event_created < rec.last_event_created:
        return

    items = (sub.get("items") or {}).get("data") or []
    first = items[0] if items else {}
    price_id = (first.get("price") or {}).get("id")
    period_end = sub.get("current_period_end") or first.get("current_period_end")

    rec.stripe_subscription_id = sub_id or rec.stripe_subscription_id
    rec.stripe_customer_id = sub.get("customer") or rec.stripe_customer_id
    rec.status = _STATUS_MAP.get(sub.get("status", ""), SubStatus.INCOMPLETE)
    rec.current_period_end = _ts(period_end)
    rec.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    plan = Plan.objects.filter(stripe_price_id=price_id).first() if price_id else None
    if plan is not None:
        rec.plan = plan
    if event_created:
        rec.last_event_created = event_created
    rec.save()
