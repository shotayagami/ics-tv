# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開 番組詳細 API (#Phase2c)。詳細ページの動的アクション部 (状態別CTA + 会員操作)。

本文 (タイトル/あらすじ/出演者/今後の放送) は SEO のため SSR のまま残し、ここでは
React 島が操作バーを描くのに必要な状態だけを返す。`public_program_detail` view と
状態判定ロジックを揃える (live/aired/upcoming・見逃し再生・YouTube アーカイブ)。

閲覧は公開 (auth=None)。お気に入り/リマインドのトグルは member_any_auth (session or
アプリの Bearer トークン、#MOBILE-01) の POST エンドポイントとして提供する。既存の
member AJAX ビュー (/members/{favorites,reminders}/<id>/toggle/) は web の SSR 島が
引き続き使うため残しており、こちらは Favorite/Reminder モデルを共有する別経路。
"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router

from api.auth import member_any_auth
from api.schemas import FavoriteOut, ProgramDetailOut, ReminderOut

router = Router(tags=["detail"])

_WD = ["月", "火", "水", "木", "金", "土", "日"]


def _pill(dt) -> str:
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]}) {lt:%H:%M} 放送予定"


@router.get("/program/{int:program_id}", response=ProgramDetailOut, auth=None)
def program_detail(request: HttpRequest, program_id: int):
    """番組詳細の操作バー状態。SSR view と同じ可視性ゲート・状態判定を使う。"""
    from scheduling import vod as vod_mod
    from scheduling.models import Program

    now = timezone.now()
    program = get_object_or_404(
        Program.objects.select_related("channel", "series", "asset"),
        pk=program_id,
        public_visible=True,
    )
    if program.start_at <= now < program.end_at:
        state = "live"
    elif program.end_at <= now:
        state = "aired"
    else:
        state = "upcoming"

    # 状態別 CTA (SSR template の .actions と同じ優先順位)。
    cta = {"kind": "none", "url": "", "label": "", "external": False}
    if state == "live":
        cta = {
            "kind": "live",
            "url": f"/ch/{program.channel.slug}/",
            "label": "▶ ライブで見る",
            "external": False,
        }
    elif state == "aired":
        if vod_mod.available_vod_qs(now).filter(pk=program.pk).exists():
            cta = {
                "kind": "vod",
                "url": f"/vod/{program.id}/",
                "label": "▶ 見逃し再生",
                "external": False,
            }
        elif program.type == "live":
            from youtube.archive import archive_watch_url

            yt = archive_watch_url(program)
            if yt:
                cta = {"kind": "yt", "url": yt, "label": "▶ YouTubeで見逃し", "external": True}

    # request.member は MemberAuthMiddleware が貼る SimpleLazyObject (匿名は None を包む) なので
    # `is not None` でなく truthy 判定する (truthy で mypy も None を除いて narrow される)。
    member = getattr(request, "member", None)
    is_member = bool(member)
    is_favorited = is_reminded = can_remind = False
    if member:
        from members.models import Favorite, Reminder

        is_favorited = Favorite.objects.filter(member=member, program=program).exists()
        is_reminded = Reminder.objects.filter(member=member, program=program).exists()
        can_remind = bool(member.is_verified)  # リマインドは要認証

    # #MOBILE-02: アプリのファンクラブ導線。suspended はページごと 404 になるため出さない。
    fc_creator_slug = ""
    if program.series_id:
        from fanclub.models import CreatorStatus
        from fanclub.services import creator_for_series

        creator = creator_for_series(program.series)
        if creator is not None and creator.status == CreatorStatus.ACTIVE:
            fc_creator_slug = creator.slug

    return {
        "id": program.id,
        "state": state,
        "cta": cta,
        "is_member": is_member,
        "is_favorited": is_favorited,
        "is_reminded": is_reminded,
        "can_remind": can_remind,
        "start_pill": _pill(program.start_at) if state == "upcoming" else "",
        # 共有先は番組ページ (API エンドポイント URL ではない)。
        "share_url": request.build_absolute_uri(f"/program/{program.id}/"),
        "share_title": program.title,
        "fc_creator_slug": fc_creator_slug,
    }


@router.post("/program/{int:program_id}/favorite", response=FavoriteOut, auth=member_any_auth)
def toggle_favorite(request: HttpRequest, program_id: int):
    """マイリスト登録トグル。members.views.favorite_toggle と同じ Favorite モデルを共有する。"""
    from members.models import Favorite
    from scheduling.models import Program

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    program = get_object_or_404(Program, pk=program_id, public_visible=True)
    fav = Favorite.objects.filter(member=member, program=program).first()
    if fav:
        fav.delete()
        favorited = False
    else:
        Favorite.objects.create(member=member, program=program)
        favorited = True
    return {"favorited": favorited}


@router.post("/program/{int:program_id}/remind", response=ReminderOut, auth=member_any_auth)
def toggle_reminder(request: HttpRequest, program_id: int):
    """リマインド予約トグル。members.views.reminder_toggle と同じ Reminder モデルを共有する。"""
    from members.models import Reminder
    from scheduling.models import Program

    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾く
    program = get_object_or_404(Program, pk=program_id, public_visible=True)
    rem = Reminder.objects.filter(member=member, program=program).first()
    if rem:
        rem.delete()
        reminded = False
    else:
        Reminder.objects.create(member=member, program=program)
        reminded = True
    return {"reminded": reminded}
