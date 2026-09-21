# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe Webhook の同期ロジック (#27 Phase B、FC有料ティア専用。純関数・event dict 受け)。

CreatorMembership の Stripe 系フィールドは Webhook が唯一の更新者(subscriptions.webhook と同じ
規律)。view 側は署名検証のみ行い handle_event(event) を呼ぶ。**event は plain dict であること**
(stripe>=12 の StripeObject は dict を継承しておらず `.get()` が使えない。dict 化は
stripe_gateway.construct_event が担う)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.security_log import emit as security_emit
from fanclub import services as fc_services
from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorTier,
    FcPendingInvoice,
    FcProcessedStripeEvent,
    FcSettlement,
    MembershipStatus,
    SlotContract,
    SlotContractStatus,
)
from members.models import Member

logger = logging.getLogger(__name__)

# 在籍に紐付けられなかった invoice の保留期間。これを過ぎても紐付かないものは別系統の請求とみなして捨てる。
_PENDING_INVOICE_DAYS = 30
_CANCELED_STATUSES = {"canceled", "incomplete_expired"}
_ACTIVE_STATUSES = {"active", "trialing"}


def _ts(epoch):
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch else None


def _is_stale(event_created: int, last_event_created: int | None) -> bool:
    return bool(event_created and last_event_created and event_created < last_event_created)


def _member_from_metadata(meta) -> Member | None:
    mid = (meta or {}).get("member_id")
    return Member.objects.filter(pk=mid).first() if mid else None


def _creator_from_metadata(meta) -> Creator | None:
    cid = (meta or {}).get("creator_id")
    return Creator.objects.filter(pk=cid).first() if cid else None


def _tier_from_metadata(meta) -> CreatorTier | None:
    tid = (meta or {}).get("tier_id")
    return CreatorTier.objects.filter(pk=tid).first() if tid else None


def _contract_from_metadata(meta) -> SlotContract | None:
    cid = (meta or {}).get("contract_id")
    return SlotContract.objects.filter(pk=cid).first() if cid else None


def handle_event(event) -> None:
    """署名検証済み event を処理する。event.id で冪等化 (subscriptions.webhook と同じ方式)。"""
    event_id = event.get("id") or ""
    if not event_id:
        _dispatch(event)  # id 無し (通常あり得ない) は冪等化できないので従来どおり処理
        return
    with transaction.atomic():
        _, created = FcProcessedStripeEvent.objects.get_or_create(
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
        _sync_from_checkout(obj, event_created=created)
    elif etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _sync_from_subscription(obj, event_created=created)
    elif etype == "account.updated":
        _sync_connect_account(obj)
    elif etype == "invoice.payment_succeeded":
        _record_settlement(obj)
    elif etype == "charge.dispute.created":
        _sync_dispute(obj, resolved=False)
    elif etype == "charge.dispute.closed":
        _sync_dispute(obj, resolved=True)


def _sync_from_checkout(session, event_created: int = 0) -> None:
    """加入/契約開始の起点。tier_id / contract_id を metadata に積んでいるため
    line_items 展開なしに一意特定できる。"""
    meta = session.get("metadata") or {}
    contract = _contract_from_metadata(meta)
    if contract is not None:
        _sync_contract_from_checkout(contract, session, event_created)
        return
    member = _member_from_metadata(meta)
    creator = _creator_from_metadata(meta)
    tier = _tier_from_metadata(meta)
    if member is None or creator is None or tier is None:
        return
    now = timezone.now()
    membership, _created = CreatorMembership.objects.get_or_create(
        member=member, creator=creator, defaults={"tier": tier}
    )
    # 順序ガード: 解約 (subscription.deleted) など、より新しい event を適用済みなら、遅れて届いた
    # checkout で在籍を ACTIVE に戻さない。
    if _is_stale(event_created, membership.last_event_created):
        return
    membership.tier = tier
    membership.status = MembershipStatus.ACTIVE
    membership.joined_at = membership.joined_at or now
    membership.left_at = None
    membership.stripe_customer_id = session.get("customer") or membership.stripe_customer_id
    membership.stripe_subscription_id = (
        session.get("subscription") or membership.stripe_subscription_id
    )
    # gift_expires_at は追加提供側機能の名残 (models.CreatorMembership を参照)。移行由来の
    # 残余値が残らないよう、サブスク加入時は防御的にクリアする。
    membership.gift_expires_at = None
    membership.save()
    # デジタル会員証の会員番号 (#27 §3.1)。無料加入は services.join が採番するため、
    # 有料の初回加入はこちらが唯一の採番点になる。
    fc_services.ensure_member_no(membership)
    _flush_pending_invoices(membership)


def _sync_contract_from_checkout(contract: SlotContract, session, event_created: int = 0) -> None:
    """枠サブスク(B2B)の Checkout 完了の起点。draft → active に進める。"""
    if _is_stale(event_created, contract.last_event_created):
        return
    contract.status = SlotContractStatus.ACTIVE
    contract.stripe_customer_id = session.get("customer") or contract.stripe_customer_id
    contract.stripe_subscription_id = session.get("subscription") or contract.stripe_subscription_id
    contract.save()


def _apply_tier_from_subscription(rec: CreatorMembership, sub) -> None:
    """サブスクの明細 Price から在籍ティアを同期する (#27 §3.1 ティア変更)。

    アップグレード(即時)・ダウングレード(Subscription Schedule の期末適用)・カスタマーポータル
    からのプラン変更のいずれも、最終的にはこのイベントの Price 差分として現れる。price_id が
    どのティアにも一致しないときは触らない (未知の Price で在籍ティアを壊さない fail-safe)。
    """
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return
    price = items[0].get("price") or {}
    price_id = price.get("id") if isinstance(price, dict) else price
    if not price_id:
        return
    tier = CreatorTier.objects.filter(creator_id=rec.creator_id, stripe_price_id=price_id).first()
    if tier is None:
        return
    rec.tier = tier
    # 予約したティアへ実際に切り替わった (= 期末が到来した) ので予約表示を消す。
    if rec.pending_tier_id == tier.pk:
        rec.pending_tier = None
        rec.pending_tier_effective_at = None


def _sync_from_subscription(sub, event_created: int = 0) -> None:
    """更新/解約の同期。checkout.session.completed 未着(順序前後)なら紐付け不能として静かに戻る。"""
    sub_id = sub.get("id") or ""
    meta = sub.get("metadata") or {}
    contract = _contract_from_metadata(meta)
    if contract is None and sub_id:
        contract = SlotContract.objects.filter(stripe_subscription_id=sub_id).first()
    if contract is None and sub.get("customer"):
        contract = SlotContract.objects.filter(stripe_customer_id=sub.get("customer")).first()
    if contract is not None:
        _sync_contract_from_subscription(contract, sub, event_created)
        return

    member = _member_from_metadata(meta)
    creator = _creator_from_metadata(meta)
    rec = None
    if member is not None and creator is not None:
        rec = CreatorMembership.objects.filter(member=member, creator=creator).first()
    if rec is None and sub_id:
        rec = CreatorMembership.objects.filter(stripe_subscription_id=sub_id).first()
    if rec is None and sub.get("customer"):
        rec = CreatorMembership.objects.filter(stripe_customer_id=sub.get("customer")).first()
    if rec is None:
        if creator is not None:
            logger.warning("fc webhook: unlinked subscription=%s creator=%s", sub_id, creator.pk)
        return

    # 順序ガード (#sec L-3 踏襲): 直近適用より古い event は捨てる。
    if _is_stale(event_created, rec.last_event_created):
        return

    status = sub.get("status", "")
    rec.stripe_subscription_id = sub_id or rec.stripe_subscription_id
    rec.stripe_customer_id = sub.get("customer") or rec.stripe_customer_id
    rec.current_period_end = _ts(sub.get("current_period_end"))
    rec.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    if event_created:
        rec.last_event_created = event_created
    _apply_tier_from_subscription(rec, sub)

    if status in _CANCELED_STATUSES or sub.get("ended_at"):
        rec.status = MembershipStatus.LEFT
        rec.left_at = timezone.now()
    elif status in _ACTIVE_STATUSES:
        rec.status = MembershipStatus.ACTIVE
    # past_due/unpaid 等は在籍のまま様子見 (Stripe が最終的に deleted を送る)
    rec.save()
    _flush_pending_invoices(rec)


def _sync_contract_from_subscription(contract: SlotContract, sub, event_created: int = 0) -> None:
    """枠サブスクの更新/解約の同期(_sync_from_subscription の CreatorMembership 分岐と同型)。"""
    sub_id = sub.get("id") or ""
    # 順序ガード (#sec L-3 踏襲): 直近適用より古い event は捨てる。
    if (
        event_created
        and contract.last_event_created
        and event_created < contract.last_event_created
    ):
        return

    status = sub.get("status", "")
    contract.stripe_subscription_id = sub_id or contract.stripe_subscription_id
    contract.stripe_customer_id = sub.get("customer") or contract.stripe_customer_id
    contract.current_period_end = _ts(sub.get("current_period_end"))
    contract.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    if event_created:
        contract.last_event_created = event_created

    if status in _CANCELED_STATUSES or sub.get("ended_at"):
        contract.status = SlotContractStatus.ENDED
    elif status in _ACTIVE_STATUSES:
        contract.status = SlotContractStatus.ACTIVE
    # past_due/unpaid 等はそのまま様子見 (Stripe が最終的に deleted を送る)
    contract.save()


def _sync_connect_account(account) -> None:
    account_id = account.get("id") or ""
    if not account_id:
        return
    creator = Creator.objects.filter(stripe_connect_account_id=account_id).first()
    if creator is None:
        return
    onboarded = bool(account.get("charges_enabled")) and bool(account.get("details_submitted"))
    if creator.stripe_connect_onboarded != onboarded:
        creator.stripe_connect_onboarded = onboarded
        creator.save(update_fields=["stripe_connect_onboarded"])


def _hold_invoice(invoice_id: str, invoice, sub_id: str, customer_id: str) -> None:
    """紐付けられなかった invoice を保留する。精算に要る項目だけを持つ (個人情報は持たない)。"""
    if (invoice.get("amount_paid") or 0) <= 0:
        return
    lines = (invoice.get("lines") or {}).get("data") or []
    period = (lines[0].get("period") if lines else None) or {}
    FcPendingInvoice.objects.get_or_create(
        stripe_invoice_id=invoice_id,
        defaults={
            "stripe_subscription_id": sub_id,
            "stripe_customer_id": customer_id,
            "payload": {
                "id": invoice_id,
                "subscription": sub_id,
                "customer": customer_id,
                "amount_paid": invoice.get("amount_paid"),
                "currency": invoice.get("currency"),
                "charge": invoice.get("charge") or "",
                "lines": {
                    "data": [{"period": {"start": period.get("start"), "end": period.get("end")}}]
                },
            },
        },
    )
    cutoff = timezone.now() - timedelta(days=_PENDING_INVOICE_DAYS)
    dropped, _ = FcPendingInvoice.objects.filter(created_at__lt=cutoff).delete()
    if dropped:
        logger.warning(
            "fc webhook: dropped %d pending invoices older than %d days",
            dropped,
            _PENDING_INVOICE_DAYS,
        )


def _flush_pending_invoices(membership: CreatorMembership) -> None:
    """在籍が確定したので、その購読・Customer の保留 invoice を精算行へ変換する。"""
    keys = Q()
    if membership.stripe_subscription_id:
        keys |= Q(stripe_subscription_id=membership.stripe_subscription_id)
    if membership.stripe_customer_id:
        keys |= Q(stripe_customer_id=membership.stripe_customer_id)
    if not keys:
        return
    for pending in FcPendingInvoice.objects.filter(keys).order_by("created_at"):
        _record_settlement(pending.payload, hold=False)
        if FcSettlement.objects.filter(stripe_invoice_id=pending.stripe_invoice_id).exists():
            pending.delete()


def _record_settlement(invoice, *, hold: bool = True) -> None:
    """成功した請求を分配元帳(FcSettlement)に記録する。サイト全体サブスク等FC以外のinvoiceは
    紐付け不能として無視する(subscriptions.MemberSubscription はここでは検索しない)。
    """
    invoice_id = invoice.get("id") or ""
    if not invoice_id or FcSettlement.objects.filter(stripe_invoice_id=invoice_id).exists():
        return  # event.id 冪等化の外側の保険(念のための二重防止)
    sub_id = invoice.get("subscription") or ""
    customer_id = invoice.get("customer") or ""
    membership = None
    if sub_id:
        membership = (
            CreatorMembership.objects.filter(stripe_subscription_id=sub_id)
            .select_related("creator", "tier", "member")
            .first()
        )
    if membership is None and customer_id:
        membership = (
            CreatorMembership.objects.filter(stripe_customer_id=customer_id)
            .select_related("creator", "tier", "member")
            .first()
        )
    if membership is None:
        logger.warning("fc webhook: unlinked invoice=%s subscription=%s (held)", invoice_id, sub_id)
        if hold:
            _hold_invoice(invoice_id, invoice, sub_id, customer_id)
        return

    gross = invoice.get("amount_paid") or 0
    if gross <= 0:
        return
    # 通貨のガード (#sec L-4/L-5 の「通貨をサーバ側検証」)。
    #
    # Checkout 作成時は currency=jpy を明示しているが、**戻りの webhook では通貨を
    # 検証していなかった**。サブスクの Price は Stripe ダッシュボード側で通貨が決まるため、
    # 誤って外貨建ての Price を紐付けると、amount_paid がそのまま「円」として台帳に載る
    # (例: 1000 USD が ¥1,000 になり、実際の入金と 2 桁ずれる)。
    #
    # 本システムは JPY 単一の設計 (金額はすべて円建ての IntegerField、会員登録も国内専用) で、
    # 多通貨に対応するには会員登録側の見直しから要る。よってここでは換算せず
    # **fail-closed で記録を拒否**し、気付けるようにする。無視して素通りさせると
    # 台帳が静かに壊れ、後から復元できない。
    currency = (invoice.get("currency") or "").lower()
    if currency and currency != "jpy":
        security_emit(
            "fanclub.stripe_webhook",
            outcome="currency_mismatch",
            level=logging.ERROR,
            invoice_id=invoice_id,
            currency=currency,
            amount=gross,
        )
        logger.error(
            "fc settlement rejected: unexpected currency=%s invoice=%s", currency, invoice_id
        )
        return
    # Stripe の実際の手数料額(application_fee_amount)は invoice からは直接得られないため、
    # Checkout 時に指定した設定値から再計算する(常に同じ割合を要求しているため厳密に一致する)。
    fee = round(gross * settings.STRIPE_CONNECT_APPLICATION_FEE_PERCENT / 100)
    net = gross - fee
    lines = (invoice.get("lines") or {}).get("data") or []
    period = lines[0].get("period") if lines else None

    FcSettlement.objects.create(
        creator=membership.creator,
        member=membership.member,
        tier=membership.tier,
        stripe_invoice_id=invoice_id,
        stripe_charge_id=invoice.get("charge") or "",
        gross_amount_minor=gross,
        application_fee_minor=fee,
        net_amount_minor=net,
        period_start=_ts(period.get("start")) if period else None,
        period_end=_ts(period.get("end")) if period else None,
    )


def _sync_dispute(dispute, *, resolved: bool) -> None:
    """チャージバック(異議申立て)の追跡(#27 Phase B、docs/fanclub.md §3.3 Should)。

    charge.dispute.created で該当 FcSettlement を disputed=True にし、
    charge.dispute.closed(status=="won")で解消、それ以外(lost等)は disputed のまま残す。
    """
    charge_id = dispute.get("charge") or ""
    if not charge_id:
        return
    settlement = FcSettlement.objects.filter(stripe_charge_id=charge_id).first()
    if settlement is None:
        return
    disputed = not (resolved and dispute.get("status") == "won")
    if settlement.disputed != disputed:
        settlement.disputed = disputed
        settlement.save(update_fields=["disputed"])
