# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ファンクラブ 参加/退会/決済 (公開ホスト /fanclub/<slug>/...)。"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.security_log import emit as security_emit
from fanclub import services
from fanclub import stripe_gateway as gw
from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorStatus,
    CreatorTier,
    FcChatMessage,
    MembershipStatus,
)
from fanclub.webhook import handle_event
from members.decorators import member_login_required

logger = logging.getLogger(__name__)


def _safe_next(request, default="/") -> str:
    """オープンリダイレクト防止 (members/views.py の _safe_next と同じ方式)。

    confirm() は GET なので request.GET も見る (POST 系ビューでは通常 GET 側は空)。
    """
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return default


def _base_url() -> str:
    return (getattr(settings, "ICSTV_PUBLIC_BASE_URL", "") or "").rstrip("/")


def tokushoho(request, creator_slug: str) -> HttpResponse:
    """クリエイター単位の特定商取引法に基づく表記 (#27 Phase B、公開・認証不要)。

    有料ティアはクリエイター単位の契約(F2)のため、運営(ICS-TV)の /tokushoho/ とは別に
    クリエイターごとに必要。価格は CreatorTier (有料のみ) から。
    """
    creator = get_object_or_404(Creator, slug=creator_slug)
    tiers = creator.tiers.filter(is_active=True, price_minor__isnull=False).order_by("level")
    return render(request, "public/fc_tokushoho.html", {"creator": creator, "tiers": tiers})


@member_login_required
@require_POST
def join(request, creator_slug: str):
    creator = get_object_or_404(Creator, slug=creator_slug, status=CreatorStatus.ACTIVE)
    next_url = _safe_next(request)
    try:
        services.join_free_tier(request.member, creator)
    except services.TierNotJoinableError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{creator.name} のファンクラブ(無料)に参加しました。")
    return redirect(next_url)


@member_login_required
def checkout_confirm(request, creator_slug: str, tier_id: int):
    """特商法対応の申込み最終確認画面 (§5.2)。実際の Checkout セッション作成は checkout() が行う。"""
    creator = get_object_or_404(Creator, slug=creator_slug, status=CreatorStatus.ACTIVE)
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator=creator, is_active=True)
    next_url = _safe_next(request)
    if not services.tier_is_joinable(tier):
        messages.error(request, "このプランはまだ準備中です。")
        return redirect(next_url)
    existing = CreatorMembership.objects.filter(
        member=request.member, creator=creator, status=MembershipStatus.ACTIVE
    ).first()
    if existing and existing.tier.level > 0 and existing.stripe_subscription_id:
        messages.info(
            request, "すでにご加入中です。プラン変更・解約は「ご契約の管理」から行えます。"
        )
        return redirect(next_url)
    return render(
        request,
        "public/fc_checkout_confirm.html",
        {"creator": creator, "tier": tier, "next": next_url},
    )


@member_login_required
@require_POST
def checkout(request, creator_slug: str, tier_id: int):
    """有料ティアへの加入。Stripe Checkout(destination charge)へリダイレクトする。

    実際の CreatorMembership 作成/更新は Webhook のみが行う(join() は無料ティア専用)。
    """
    creator = get_object_or_404(Creator, slug=creator_slug, status=CreatorStatus.ACTIVE)
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator=creator, is_active=True)
    next_url = _safe_next(request)
    if not services.tier_is_joinable(tier):
        messages.error(request, "このプランはまだ準備中です。")
        return redirect(next_url)
    existing = CreatorMembership.objects.filter(
        member=request.member, creator=creator, status=MembershipStatus.ACTIVE
    ).first()
    # 既に同クリエイターの有料ティアに在籍中なら新規 Checkout を作らない(二重課金防止・M-3踏襲)。
    # プラン変更/解約は Stripe カスタマーポータルへ誘導する。
    if existing and existing.tier.level > 0 and existing.stripe_subscription_id:
        messages.info(
            request, "すでにご加入中です。プラン変更・解約は「ご契約の管理」から行えます。"
        )
        return redirect(next_url)
    try:
        url = gw.create_checkout_session(
            customer_id=gw.ensure_customer(request.member, existing),
            price_id=tier.stripe_price_id,
            connect_account_id=creator.stripe_connect_account_id,
            application_fee_percent=settings.STRIPE_CONNECT_APPLICATION_FEE_PERCENT,
            success_url=f"{_base_url()}{next_url}?fc_success=1",
            cancel_url=f"{_base_url()}{next_url}?fc_canceled=1",
            member_id=request.member.pk,
            creator_id=creator.pk,
            tier_id=tier.pk,
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception(
            "fc stripe checkout failed: member=%s creator=%s tier=%s",
            request.member.pk,
            creator.pk,
            tier.pk,
        )
        messages.error(request, "決済ページの作成に失敗しました。時間をおいてお試しください。")
        return redirect(next_url)
    return redirect(url)


def creator_page(request, slug: str):
    """公開ファンクラブページ /fc/<slug>/ (#27 §10.2 Must)。

    無料公開レイヤー: プロフィール/番組/放送予定は誰でも見える。会員ゲート: 投稿本文は
    fc_required_level と在籍ティアで series 詳細と同じロック判定 (プレビューとして
    タイトルだけ出す)。suspended は 404 (fail-closed、公開ページごと消す)。
    """
    from scheduling.models import Program, Series, SeriesPost

    creator = get_object_or_404(Creator, slug=slug, status=CreatorStatus.ACTIVE)
    member = getattr(request, "member", None)
    now = timezone.now()

    series_ids = list(creator.series_links.values_list("series_id", flat=True))
    series_list = list(Series.objects.filter(pk__in=series_ids, is_active=True).order_by("title"))
    upcoming = (
        Program.objects.filter(series_id__in=series_ids, public_visible=True, start_at__gte=now)
        .select_related("channel", "series")
        .order_by("start_at")[:6]
    )
    posts = list(
        SeriesPost.objects.filter(series_id__in=series_ids, is_published=True)
        .select_related("series")
        .order_by("-published_at", "-id")[:20]
    )
    my_level = services.member_level(member, creator)
    for post in posts:
        post.locked = post.fc_required_level is not None and not services.can_view_level(
            post.fc_required_level, member, creator
        )
    my_membership, fc_tiers = services.tier_rows(member, creator)
    # 会員限定チャット (#27 §3.1 Should)。資格がある会員にのみ直近50件を SSR し、以降は WS。
    can_chat = services.can_use_chat(member, creator)
    chat_messages = []
    if can_chat:
        chat_messages = list(
            FcChatMessage.objects.filter(creator=creator, deleted_at__isnull=True)
            .select_related("member")
            .order_by("-created_at")[:50]
        )[::-1]
        badge_by_member = {
            m.member_id: m.loyalty_badge
            for m in CreatorMembership.objects.filter(
                creator=creator,
                status=MembershipStatus.ACTIVE,
                member_id__in={msg.member_id for msg in chat_messages},
            )
        }
        for msg in chat_messages:
            msg.badge = badge_by_member.get(msg.member_id, "")
    return render(
        request,
        "public/fc_page.html",
        {
            "creator": creator,
            "theme_color": services.safe_theme_color(creator),
            "series_list": series_list,
            "upcoming": upcoming,
            "posts": posts,
            "my_level": my_level,
            "my_membership": my_membership,
            "fc_tiers": fc_tiers,
            "can_chat": can_chat,
            "chat_messages": chat_messages,
            "now": now,
        },
    )


_CHAT_COOLDOWN = 5  # 連投クールダウン秒 (members.views の comment と同じ)
_CHAT_MAX_LEN = 500


def _chat_payload(msg) -> dict:
    """チャット発言の dict 表現 (SSR/WS 共通)。body は生テキスト (描画側でエスケープ)。"""
    lt = timezone.localtime(msg.created_at)
    membership = CreatorMembership.objects.filter(
        member_id=msg.member_id, creator_id=msg.creator_id, status=MembershipStatus.ACTIVE
    ).first()
    return {
        "id": msg.pk,
        "member_id": msg.member_id,
        "nickname": msg.member.nickname,
        "badge": membership.loyalty_badge if membership else "",
        "time": f"{lt.month}/{lt.day} {lt:%H:%M}",
        "body": msg.body,
    }


def _broadcast_chat_message(creator, msg) -> None:
    """新規発言を WS group へ配信。best-effort (members.views._broadcast_new_comment と同型)。"""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from fanclub.consumers import chat_group

    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            chat_group(creator.slug),
            {"type": "chat.new", "message": _chat_payload(msg)},
        )
    except Exception:
        logger.warning("fc chat broadcast failed", exc_info=True)


@member_login_required
@require_POST
def chat_post(request, creator_slug: str):
    """会員限定チャットへの投稿 (#27 §3.1 Should)。投稿=HTTP / 配信=WS (#COMM-01 と同じ分離)。"""
    creator = get_object_or_404(Creator, slug=creator_slug, status=CreatorStatus.ACTIVE)
    if not services.can_use_chat(request.member, creator):
        return HttpResponseForbidden("チャットに参加するにはファンクラブへの加入が必要です。")
    if not request.member.is_verified:
        messages.error(request, "チャットに参加するには本人確認が必要です。")
        return redirect(f"/fc/{creator.slug}/#fc-chat")
    body = (request.POST.get("body") or "").strip()
    if not body:
        return redirect(f"/fc/{creator.slug}/#fc-chat")
    last = (
        FcChatMessage.objects.filter(member=request.member, creator=creator)
        .order_by("-created_at")
        .first()
    )
    if last and (timezone.now() - last.created_at).total_seconds() < _CHAT_COOLDOWN:
        messages.error(request, "投稿の間隔が短すぎます。少し待ってからお試しください。")
        return redirect(f"/fc/{creator.slug}/#fc-chat")
    msg = FcChatMessage.objects.create(
        creator=creator, member=request.member, body=body[:_CHAT_MAX_LEN]
    )
    _broadcast_chat_message(creator, msg)
    return redirect(f"/fc/{creator.slug}/#fc-chat")


@member_login_required
@require_POST
def chat_delete(request, creator_slug: str, message_id: int):
    """自分の発言の削除 (soft delete)。members.views.comment_delete と同型。"""
    creator = get_object_or_404(Creator, slug=creator_slug)
    msg = get_object_or_404(FcChatMessage, pk=message_id, creator=creator, deleted_at__isnull=True)
    if msg.member_id != request.member.pk:
        return HttpResponseForbidden("自分の発言のみ削除できます。")
    FcChatMessage.objects.filter(pk=msg.pk).update(deleted_at=timezone.now())
    return redirect(f"/fc/{creator.slug}/#fc-chat")


def _change_target(
    request, creator_slug: str, tier_id: int
) -> tuple[Creator, CreatorTier, CreatorMembership, str] | None:
    """プラン変更系ビュー共通の前段。成立しない要求は messages を積んで None を返す。"""
    creator = get_object_or_404(Creator, slug=creator_slug, status=CreatorStatus.ACTIVE)
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator=creator, is_active=True)
    membership = services.active_paid_membership(request.member, creator)
    if membership is None:
        messages.error(request, "有料プランをご利用中の方のみプラン変更できます。")
        return None
    try:
        direction = services.plan_tier_change(membership, tier)
    except services.FanclubError as exc:
        messages.error(request, str(exc))
        return None
    return creator, tier, membership, direction


@member_login_required
def change_confirm(request, creator_slug: str, tier_id: int):
    """プラン変更の最終確認画面。プラン変更も申込みに当たるため確認画面を挟む (§5.2)。"""
    next_url = _safe_next(request)
    target = _change_target(request, creator_slug, tier_id)
    if target is None:
        return redirect(next_url)
    creator, tier, membership, direction = target
    return render(
        request,
        "public/fc_change_confirm.html",
        {
            "creator": creator,
            "tier": tier,
            "membership": membership,
            "direction": direction,
            "next": next_url,
        },
    )


@member_login_required
@require_POST
def change(request, creator_slug: str, tier_id: int):
    """有料ティア間の変更 (アップグレード=即時+日割り / ダウングレード=期末適用、F5)。"""
    next_url = _safe_next(request)
    target = _change_target(request, creator_slug, tier_id)
    if target is None:
        return redirect(next_url)
    creator, tier, membership, _direction = target
    try:
        direction, effective_at = services.change_tier(membership, tier)
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except services.FanclubError as exc:
        messages.error(request, str(exc))
        return redirect(next_url)
    except Exception:
        logger.exception(
            "fc tier change failed: member=%s creator=%s tier=%s",
            request.member.pk,
            creator.pk,
            tier.pk,
        )
        messages.error(request, "プラン変更に失敗しました。時間をおいてお試しください。")
        return redirect(next_url)
    if direction == "upgrade":
        messages.success(
            request, f"プランを「{tier.name}」へ変更しました。差額は日割りで請求されます。"
        )
    else:
        when = timezone.localtime(effective_at).strftime("%Y年%m月%d日")
        messages.success(
            request, f"{when} から「{tier.name}」へ変更されます。それまでは現在のプランのままです。"
        )
    return redirect(next_url)


@member_login_required
@require_POST
def change_cancel(request, creator_slug: str):
    """ダウングレード予約の取り消し。"""
    creator = get_object_or_404(Creator, slug=creator_slug)
    next_url = _safe_next(request)
    membership = services.active_paid_membership(request.member, creator)
    if membership is None or membership.pending_tier_id is None:
        return redirect(next_url)
    try:
        services.cancel_tier_change(membership)
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception(
            "fc tier change cancel failed: member=%s creator=%s", request.member.pk, creator.pk
        )
        messages.error(request, "変更予約の取り消しに失敗しました。")
        return redirect(next_url)
    messages.info(request, "プラン変更の予約を取り消しました。")
    return redirect(next_url)


@member_login_required
@require_POST
def portal(request, creator_slug: str):
    """有料ティアの解約/カード変更。Stripe カスタマーポータルへ誘導する。"""
    creator = get_object_or_404(Creator, slug=creator_slug)
    next_url = _safe_next(request)
    membership = CreatorMembership.objects.filter(
        member=request.member, creator=creator, status=MembershipStatus.ACTIVE
    ).first()
    if not membership or not membership.stripe_customer_id:
        return redirect(next_url)
    try:
        url = gw.create_portal_session(
            customer_id=membership.stripe_customer_id,
            return_url=f"{_base_url()}{next_url}",
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception(
            "fc stripe portal failed: member=%s creator=%s", request.member.pk, creator.pk
        )
        messages.error(request, "管理ページを開けませんでした。")
        return redirect(next_url)
    return redirect(url)


@member_login_required
@require_POST
def leave(request, creator_slug: str):
    """退会。有料ティア(Stripeサブスク在)は DB を直接いじらず解約導線(portal)へ誘導する
    (状態は Webhook のみが更新する規律を保つ。ここで status=left にすると Stripe 側は
    課金され続けたままDBだけ退会扱いになり実害が出る)。
    """
    creator = get_object_or_404(Creator, slug=creator_slug)
    next_url = _safe_next(request)
    membership = CreatorMembership.objects.filter(
        member=request.member, creator=creator, status=MembershipStatus.ACTIVE
    ).first()
    if membership and membership.tier.level > 0 and membership.stripe_subscription_id:
        return portal(request, creator_slug)
    services.leave(request.member, creator)
    messages.info(request, f"{creator.name} のファンクラブを退会しました。")
    return redirect(next_url)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe Webhook。署名検証 → handle_event。署名NG=400、ハンドラ例外=500(Stripeが再送)。"""
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = gw.construct_event(request.body, sig)
    except gw.StripeNotConfiguredError:
        return HttpResponse("webhook 未設定", status=503)
    except Exception:
        security_emit("fanclub.stripe_webhook", outcome="signature_fail", request=request)
        return HttpResponseBadRequest("invalid signature")
    try:
        handle_event(event)
    except Exception:
        logger.exception("fc stripe webhook handler error")
        security_emit(
            "fanclub.stripe_webhook",
            outcome="handler_error",
            event_type=(event.get("type") if isinstance(event, dict) else None),
        )
        return HttpResponse(status=500)
    return JsonResponse({"received": True})
