# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスク 会員ページ + Stripe Checkout/Portal/Webhook (公開ホスト)。"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.security_log import emit as security_emit
from members.decorators import member_login_required
from subscriptions import stripe_gateway as gw
from subscriptions.decorators import subscription_required
from subscriptions.models import MemberSubscription, Plan
from subscriptions.services import get_subscription
from subscriptions.webhook import handle_event

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return (getattr(settings, "ICSTV_PUBLIC_BASE_URL", "") or "").rstrip("/")


@member_login_required
def subscription_page(request):
    sub = get_subscription(request.member)
    return render(
        request,
        "subscriptions/page.html",
        {
            "sub": sub,
            "plans": Plan.objects.filter(is_active=True).order_by("rank"),
            "configured": bool(settings.STRIPE_SECRET_KEY),
        },
    )


@member_login_required
def checkout_confirm(request, slug):
    """特商法対応の申込み最終確認画面 (§5.2)。実際の Checkout セッション作成は checkout() が行う。"""
    plan = get_object_or_404(Plan, slug=slug, is_active=True)
    sub = get_subscription(request.member)
    if sub and sub.is_active:
        messages.info(
            request, "すでにご契約中です。プラン変更・解約は「ご契約の管理」から行えます。"
        )
        return redirect("/subscriptions/")
    if not settings.STRIPE_SECRET_KEY or not plan.stripe_price_id:
        messages.error(request, "このプランはまだ購入できません。")
        return redirect("/subscriptions/")
    return render(request, "subscriptions/confirm.html", {"plan": plan})


@member_login_required
@require_POST
def checkout(request, slug):
    plan = get_object_or_404(Plan, slug=slug, is_active=True)
    sub = get_subscription(request.member)
    # 既に有効サブスクがあるなら新規 Checkout を作らない (二重サブスク=二重課金の防止・M-3)。
    # プラン変更・解約は Stripe カスタマーポータル (ご契約の管理) へ誘導する。
    if sub and sub.is_active:
        messages.info(
            request, "すでにご契約中です。プラン変更・解約は「ご契約の管理」から行えます。"
        )
        return redirect("/subscriptions/")
    if not plan.stripe_price_id:
        messages.error(request, "このプランはまだ購入できません。")
        return redirect("/subscriptions/")
    try:
        customer_id = gw.ensure_customer(request.member, sub)
        # 顧客IDを先に紐付け (checkout 離脱しても customer を再利用できる)
        MemberSubscription.objects.update_or_create(
            member=request.member, defaults={"stripe_customer_id": customer_id}
        )
        url = gw.create_checkout_session(
            customer_id=customer_id,
            price_id=plan.stripe_price_id,
            success_url=f"{_base_url()}/subscriptions/?success=1",
            cancel_url=f"{_base_url()}/subscriptions/?canceled=1",
            member_id=request.member.pk,
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception("stripe checkout failed: member=%s", request.member.pk)
        messages.error(request, "決済ページの作成に失敗しました。時間をおいてお試しください。")
        return redirect("/subscriptions/")
    return redirect(url)


@subscription_required
def exclusive(request):
    """会員限定コンテンツ (= サブスク限定の見逃し配信) 一覧。feat_exclusive の実体。

    @subscription_required を通った会員はサブスク限定 VOD を全て視聴できる。
    再生自体は公開 /vod/<id>/ が可視性ゲートで担保する。
    """
    from scheduling.models import VodVisibility
    from scheduling.vod import available_vod_qs

    items = list(available_vod_qs().filter(vod_visibility=VodVisibility.SUBSCRIBERS)[:60])
    return render(request, "subscriptions/exclusive.html", {"items": items})


@member_login_required
@require_POST
def portal(request):
    sub = get_subscription(request.member)
    if not sub or not sub.stripe_customer_id:
        return redirect("/subscriptions/")
    try:
        url = gw.create_portal_session(
            customer_id=sub.stripe_customer_id, return_url=f"{_base_url()}/subscriptions/"
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception("stripe portal failed: member=%s", request.member.pk)
        messages.error(request, "管理ページを開けませんでした。")
        return redirect("/subscriptions/")
    return redirect(url)


@csrf_exempt
@require_POST
def webhook(request):
    """Stripe Webhook。署名検証 → handle_event。署名 NG=400、ハンドラ例外=500(Stripe が再送)。"""
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = gw.construct_event(request.body, sig)
    except gw.StripeNotConfiguredError:
        return HttpResponse("webhook 未設定", status=503)
    except Exception:
        # 署名検証失敗は通常 0 件。急増は偽 webhook / secret 不整合の兆候 (#sec §3)。
        security_emit("billing.stripe_webhook", outcome="signature_fail", request=request)
        return HttpResponseBadRequest("invalid signature")
    try:
        handle_event(event)
    except Exception:
        logger.exception("stripe webhook handler error")
        security_emit(
            "billing.stripe_webhook",
            outcome="handler_error",
            event_type=(event.get("type") if isinstance(event, dict) else None),
        )
        return HttpResponse(status=500)
    return JsonResponse({"received": True})
