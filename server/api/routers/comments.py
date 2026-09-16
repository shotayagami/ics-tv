# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開プレイヤー島向け コメント API (#Phase1 / #COMM-01)。

閲覧=公開 / 投稿=確認済み会員 / 削除=本人 / pin=会員。投稿は WS ブロードキャスト
(_broadcast_new_comment) を伴う。認証は session cookie + CSRF (api.auth.member_auth) と
アプリの Bearer トークン (api.auth.member_any_auth、#MOBILE-01) の両方を受ける。
クールダウン/特典/番組名スナップショットは既存 members.views のロジックを再利用する。
エラーは HttpError(status, detail) で返す (ninja 既定の {"detail": ...})。
"""

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from api.auth import member_any_auth
from api.schemas import CommentCreateIn, CommentItem, CommentListOut, OkOut, PinOut

router = Router(tags=["comments"])


@router.get("/channels/{slug}/comments", response=CommentListOut, auth=None)
def list_comments(request: HttpRequest, slug: str):
    from core.models import Channel
    from members.models import Comment
    from members.serializers import comment_payload
    from subscriptions.services import comment_perk_member_ids

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    member = getattr(request, "member", None)
    comments = list(
        Comment.objects.filter(channel=channel, deleted_at__isnull=True)
        .select_related("member")
        .order_by("-created_at")[:50]
    )
    badge_ids = comment_perk_member_ids({c.member_id for c in comments})
    items = [comment_payload(c, badge=c.member_id in badge_ids) for c in comments]
    if member and member.is_verified:
        gate = "ok"
    elif member:
        gate = "verify"
    else:
        gate = "login"
    return {
        "count": len(items),
        "can_post": gate == "ok",
        "gate": gate,
        "nickname": member.nickname if member else "",
        "me_member_id": member.pk if member else None,
        "items": items,
    }


@router.post("/channels/{slug}/comments", response=CommentItem, auth=member_any_auth)
def post_comment(request: HttpRequest, slug: str, payload: CommentCreateIn):
    from core.models import Channel
    from members.models import Comment
    from members.serializers import comment_payload
    from members.views import (
        _COMMENT_COOLDOWN,
        _COMMENT_COOLDOWN_PERK,
        _COMMENT_MAX_LEN,
        _broadcast_new_comment,
        _current_program_title,
    )
    from subscriptions.models import ENT_COMMENT_PERK
    from subscriptions.services import entitlements

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾くため到達時は非 None
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    if not member.is_verified:
        raise HttpError(403, "コメントするには本人確認が必要です。")
    body = (payload.body or "").strip()
    if not body:
        raise HttpError(400, "コメントを入力してください。")
    has_perk = ENT_COMMENT_PERK in entitlements(member)
    cooldown = _COMMENT_COOLDOWN_PERK if has_perk else _COMMENT_COOLDOWN
    last = Comment.objects.filter(member=member).order_by("-created_at").first()
    if last and (timezone.now() - last.created_at).total_seconds() < cooldown:
        raise HttpError(429, "投稿が早すぎます。少し待ってからお試しください。")
    comment = Comment.objects.create(
        channel=channel,
        member=member,
        body=body[:_COMMENT_MAX_LEN],
        program_title=_current_program_title(channel),
    )
    _broadcast_new_comment(channel, comment, badge=has_perk)  # #COMM-01 リアルタイム配信
    return comment_payload(comment, badge=has_perk)


@router.delete("/channels/{slug}/comments/{cid}", response=OkOut, auth=member_any_auth)
def delete_comment(request: HttpRequest, slug: str, cid: int):
    from members.models import Comment

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    comment = Comment.objects.filter(pk=cid, deleted_at__isnull=True).first()
    if comment is None:
        raise HttpError(404, "コメントが見つかりません。")
    if comment.member_id != member.pk:
        raise HttpError(403, "自分のコメントのみ削除できます。")
    Comment.objects.filter(pk=comment.pk).update(deleted_at=timezone.now())
    return {"ok": True}


@router.post("/channels/{slug}/pin", response=PinOut, auth=member_any_auth)
def toggle_pin(request: HttpRequest, slug: str):
    from core.models import Channel
    from members.models import ChannelFavorite

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    fav = ChannelFavorite.objects.filter(member=member, channel=channel).first()
    if fav:
        fav.delete()
        pinned = False
    else:
        ChannelFavorite.objects.create(member=member, channel=channel)
        pinned = True
    return {"pinned": pinned}
