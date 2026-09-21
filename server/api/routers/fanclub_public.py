# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開ファンクラブ API (#MOBILE-02)。アプリが /fc/<slug>/ 相当の画面を描くための島。

web (SSR) と同じ判定ロジック (fanclub.services) を共有する。決済を伴う操作
(有料ティアの加入/変更) はアプリ内では提供せず、FcPageOut.web_url への
リンクアウトに倒す (Google Play の決済ポリシー + Stripe Checkout フローの再実装回避)。
無料ティアの加入/退会・会員限定チャットは決済を伴わないためネイティブで完結させる。

閲覧 (fc ページ) は auth=None だが、MemberAuthMiddleware が Bearer/session から
request.member を解決するため、ログイン済みなら在籍状態 (viewer) を含めて返せる。
"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from api.auth import member_any_auth
from api.schemas import (
    FcChatCreateIn,
    FcChatItem,
    FcChatListOut,
    FcJoinFreeOut,
    FcMyOut,
    FcPageOut,
    OkOut,
)

router = Router(tags=["fanclub"])

_WD = ["月", "火", "水", "木", "金", "土", "日"]


def _jp_dt(dt) -> str:
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]}) {lt:%H:%M}"


def _jp_date(dt) -> str:
    if dt is None:
        return ""
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]})"


def _jp_full_date(dt) -> str:
    if dt is None:
        return ""
    lt = timezone.localtime(dt)
    return f"{lt.year}年{lt.month}月{lt.day}日"


def _membership_out(membership) -> dict:
    """FcMembershipOut の dict 表現 (会員証)。未採番なら遅延採番する (members.views と同じ)。"""
    from fanclub import services as fc_services

    fc_services.ensure_member_no(membership)
    creator = membership.creator
    return {
        "creator_slug": creator.slug,
        "creator_name": creator.name,
        "avatar_url": creator.avatar_url,
        "theme_color": fc_services.safe_theme_color(creator),
        "tier_level": membership.tier.level,
        "tier_name": membership.tier.name,
        "member_no": membership.member_no_display,
        "joined_display": _jp_full_date(membership.joined_at),
        "enrolled_months": membership.enrolled_months,
        "loyalty_badge": membership.loyalty_badge,
        "pending_tier_name": membership.pending_tier.name if membership.pending_tier else "",
        "pending_effective_display": _jp_full_date(membership.pending_tier_effective_at),
    }


def _active_membership(member, creator):
    """member の creator における在籍行 (無料/有料/ギフトを問わない)。無ければ None。"""
    from fanclub.models import CreatorMembership, MembershipStatus

    if not member:
        return None
    return (
        CreatorMembership.objects.filter(
            member=member, creator=creator, status=MembershipStatus.ACTIVE
        )
        .select_related("creator", "tier", "pending_tier")
        .first()
    )


@router.get("/fc/{slug}", response=FcPageOut, auth=None)
def fc_page(request: HttpRequest, slug: str):
    """公開クリエイターページ。fanclub.views.creator_page と同じ構成要素を JSON で返す。"""
    from fanclub import services as fc_services
    from fanclub.models import Creator, CreatorStatus
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

    membership = _active_membership(member, creator)
    tiers = []
    free = creator.tiers.filter(is_active=True, level=0).first()
    if free is not None:
        on_free = membership is not None and membership.tier.level == 0
        tiers.append(
            {
                "id": free.pk,
                "level": 0,
                "name": free.name,
                "description": free.description,
                # price_jpy は **配布済み Android アプリ (v0.4.0) が直接パースしている**
                # ため残す。消すと JSON から欠けて null になり、有料ティアが「無料」と
                # 表示される (クラッシュせず静かに間違う)。多通貨を実際に売り始めるには
                # アプリ側の price_minor/currency 対応が前提。
                "price_jpy": None,
                "price_minor": None,
                "currency": "jpy",
                "action": "current" if on_free else "free_join",
            }
        )
    _, paid_rows = fc_services.tier_rows(member, creator)
    for row in paid_rows:
        t = row["tier"]
        tiers.append(
            {
                "id": t.pk,
                "level": t.level,
                "name": t.name,
                "description": t.description,
                # price_jpy は後方互換。JPY 以外では意味を持たないため null にする
                # (誤って円として表示されるより、欠測として扱われるほうが安全)。
                "price_jpy": t.price_minor if t.currency == "jpy" else None,
                "price_minor": t.price_minor,
                "currency": t.currency,
                "action": row["action"],
            }
        )

    return {
        "slug": creator.slug,
        "name": creator.name,
        "description": creator.description,
        "avatar_url": creator.avatar_url,
        "cover_url": creator.cover_url,
        "theme_color": fc_services.safe_theme_color(creator),
        "sns_x_url": creator.sns_x_url,
        "sns_youtube_url": creator.sns_youtube_url,
        "sns_instagram_url": creator.sns_instagram_url,
        "website_url": creator.website_url,
        "tiers": tiers,
        "is_member": bool(member),
        "viewer": _membership_out(membership) if membership else None,
        "chat_enabled": creator.chat_required_level is not None,
        "can_chat": fc_services.can_use_chat(member, creator),
        "chat_required_level": creator.chat_required_level,
        "upcoming": [
            {
                "program_id": p.pk,
                "title": p.title,
                "series_title": p.series.title if p.series else "",
                "channel_slug": p.channel.slug,
                "time": _jp_dt(p.start_at),
            }
            for p in upcoming
        ],
        "series": [{"id": s.pk, "title": s.title} for s in series_list],
        "posts": [
            {
                "id": post.pk,
                "series_id": post.series_id,
                "series_title": post.series.title,
                "kind": post.kind,
                "title": post.title,
                "published_at": _jp_date(post.published_at),
                "locked": post.fc_required_level is not None
                and not fc_services.can_view_level(post.fc_required_level, member, creator),
            }
            for post in posts
        ],
        "web_url": request.build_absolute_uri(f"/fc/{creator.slug}/"),
    }


@router.post("/fc/{slug}/join", response=FcJoinFreeOut, auth=member_any_auth)
def fc_join_free(request: HttpRequest, slug: str):
    """無料ティアへの加入 (fanclub.views.join と同じ services.join_free_tier)。"""
    from fanclub import services as fc_services
    from fanclub.models import Creator, CreatorStatus

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    creator = get_object_or_404(Creator, slug=slug, status=CreatorStatus.ACTIVE)
    try:
        membership = fc_services.join_free_tier(member, creator)
    except fc_services.TierNotJoinableError as exc:
        raise HttpError(400, str(exc)) from exc
    return {"ok": True, "tier_name": membership.tier.name}


@router.post("/fc/{slug}/leave", response=OkOut, auth=member_any_auth)
def fc_leave(request: HttpRequest, slug: str):
    """退会。Stripe サブスク在籍は web の解約導線 (カスタマーポータル) のみで受ける。

    fanclub.views.leave と同じ判定: DB を直接 left にすると Stripe 側は課金され続ける
    ため、有料 (サブスク) 在籍はここでは触らず 409 でリンクアウトを促す。
    """
    from fanclub import services as fc_services
    from fanclub.models import Creator

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    creator = get_object_or_404(Creator, slug=slug)
    membership = _active_membership(member, creator)
    if membership is None:
        raise HttpError(404, "このファンクラブには加入していません。")
    if membership.tier.level > 0 and membership.stripe_subscription_id:
        raise HttpError(409, "有料プランの解約は Web の「ご契約の管理」から行ってください。")
    fc_services.leave(member, creator)
    return {"ok": True}


@router.get("/members/fanclub", response=FcMyOut, auth=member_any_auth)
def my_fanclub(request: HttpRequest):
    """加入中のファンクラブ一覧 (会員証データ)。members.views.fanclub_list 相当。"""
    from members.views import _my_active_memberships

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    return {
        "memberships": [_membership_out(m) for m in _my_active_memberships(member)],
    }


_CHAT_LIMIT = 50


def _require_chat_member(request: HttpRequest, slug: str):
    """チャット系エンドポイント共通の前段。資格が無ければ 403 (web と同じ文言)。"""
    from fanclub import services as fc_services
    from fanclub.models import Creator, CreatorStatus

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    creator = get_object_or_404(Creator, slug=slug, status=CreatorStatus.ACTIVE)
    if not fc_services.can_use_chat(member, creator):
        raise HttpError(403, "チャットに参加するにはファンクラブへの加入が必要です。")
    return member, creator


@router.get("/fc/{slug}/chat", response=FcChatListOut, auth=member_any_auth)
def fc_chat_list(request: HttpRequest, slug: str):
    """直近の発言 (古い順)。アプリはコメントと同じくポーリングで追従する (WS は使わない)。"""
    from fanclub.models import CreatorMembership, FcChatMessage, MembershipStatus

    member, creator = _require_chat_member(request, slug)
    messages = list(
        FcChatMessage.objects.filter(creator=creator, deleted_at__isnull=True)
        .select_related("member")
        .order_by("-created_at")[:_CHAT_LIMIT]
    )[::-1]
    badge_by_member = {
        m.member_id: m.loyalty_badge
        for m in CreatorMembership.objects.filter(
            creator=creator,
            status=MembershipStatus.ACTIVE,
            member_id__in={msg.member_id for msg in messages},
        )
    }
    lt = timezone.localtime
    return {
        "can_post": bool(member.is_verified),
        "me_member_id": member.pk,
        "items": [
            {
                "id": msg.pk,
                "member_id": msg.member_id,
                "nickname": msg.member.nickname,
                "badge": badge_by_member.get(msg.member_id, ""),
                "time": f"{lt(msg.created_at).month}/{lt(msg.created_at).day} "
                f"{lt(msg.created_at):%H:%M}",
                "body": msg.body,
            }
            for msg in messages
        ],
    }


@router.post("/fc/{slug}/chat", response=FcChatItem, auth=member_any_auth)
def fc_chat_post(request: HttpRequest, slug: str, payload: FcChatCreateIn):
    """発言の投稿。検証は fanclub.views.chat_post と同一 (資格/本人確認/空/クールダウン)。"""
    from fanclub.models import FcChatMessage
    from fanclub.views import _CHAT_COOLDOWN, _CHAT_MAX_LEN, _broadcast_chat_message, _chat_payload

    member, creator = _require_chat_member(request, slug)
    if not member.is_verified:
        raise HttpError(403, "チャットに参加するには本人確認が必要です。")
    body = (payload.body or "").strip()
    if not body:
        raise HttpError(400, "本文を入力してください。")
    last = (
        FcChatMessage.objects.filter(member=member, creator=creator).order_by("-created_at").first()
    )
    if last and (timezone.now() - last.created_at).total_seconds() < _CHAT_COOLDOWN:
        raise HttpError(429, "投稿の間隔が短すぎます。少し待ってからお試しください。")
    msg = FcChatMessage.objects.create(creator=creator, member=member, body=body[:_CHAT_MAX_LEN])
    _broadcast_chat_message(creator, msg)  # web の WS 閲覧者へも配信
    return _chat_payload(msg)


@router.delete("/fc/{slug}/chat/{int:message_id}", response=OkOut, auth=member_any_auth)
def fc_chat_delete(request: HttpRequest, slug: str, message_id: int):
    """自分の発言の削除 (soft delete)。fanclub.views.chat_delete と同型。"""
    from fanclub.models import Creator, FcChatMessage

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    creator = get_object_or_404(Creator, slug=slug)
    msg = FcChatMessage.objects.filter(
        pk=message_id, creator=creator, deleted_at__isnull=True
    ).first()
    if msg is None:
        raise HttpError(404, "発言が見つかりません。")
    if msg.member_id != member.pk:
        raise HttpError(403, "自分の発言のみ削除できます。")
    FcChatMessage.objects.filter(pk=msg.pk).update(deleted_at=timezone.now())
    return {"ok": True}
