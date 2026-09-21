# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開ディスカバリ API (#Phase2c)。検索 / ジャンルブラウズ / 見逃し一覧。

既存 view 層 (public_search/browse/vod_list の Q 絞り込み・vod.available_vod_qs・can_watch・
youtube.recent_live_archives・_present_genres) を薄く JSON 化する。閲覧は公開 (auth=None)。
番組カード/行の表示文字列 (日時・尺・ゲート) はサーバで整形して返す。
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Status

from api.schemas import BrowseOut, SearchOut, VodOut, VodPlayOut

router = Router(tags=["discover"])

_WD = ["月", "火", "水", "木", "金", "土", "日"]


def _jp_dt(dt, suffix: str = "") -> str:
    """n/j(曜) H:i[suffix] (Django date:'n/j(D) H:i' 相当・日本語曜日)。"""
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]}) {lt:%H:%M}{suffix}"


def _card(p, *, member=None, vod: bool = False, yt: bool = False) -> dict:
    from scheduling import vod as vod_mod

    item: dict = {
        "id": p.id,
        "title": p.title,
        "channel_name": p.channel.name,
        "genre": p.resolved_genre or "",
        "start_display": _jp_dt(p.start_at, " 放送" if (vod or yt) else ""),
        "thumb_url": p.thumb_url or "",
    }
    if vod:
        item["vod_visibility"] = getattr(p, "vod_visibility", "") or ""
        item["can_watch"] = vod_mod.can_watch(p, member)
        asset = getattr(p, "asset", None)
        item["duration"] = (
            asset.duration_display if asset and getattr(asset, "duration_ms", None) else ""
        )
    if yt:
        item["badge"] = "yt"
    return item


@router.get("/search", response=SearchOut, auth=None)
def search(request: HttpRequest, q: str = ""):
    from django.db.models import Q

    from core.models import Channel
    from scheduling.models import Program

    q = q.strip()
    programs: list = []
    channels: list = []
    if q:
        match = (
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(cast__icontains=q)
            | Q(genre__icontains=q)
            | Q(series__title__icontains=q)
            | Q(series__cast__icontains=q)
        )
        programs = [
            {
                "id": p.id,
                "title": p.title,
                "channel_name": p.channel.name,
                "genre": p.resolved_genre or "",
                "start_display": _jp_dt(p.start_at),
            }
            for p in Program.objects.filter(public_visible=True)
            .filter(match)
            .select_related("channel", "series")
            .order_by("-start_at")
            .distinct()[:40]
        ]
        channels = [
            {"slug": c.slug, "name": c.name, "tint": c.tint_color}
            for c in Channel.objects.filter(enabled=True, name__icontains=q)[:8]
        ]
    return {"q": q, "programs": programs, "channels": channels}


@router.get("/browse", response=BrowseOut, auth=None)
def browse(request: HttpRequest, genre: str = ""):
    from django.db.models import Q

    from core.views import _present_genres
    from scheduling.models import Program

    genres = _present_genres()
    genre = genre.strip()
    programs: list = []
    if genre in genres:
        base_qs = (
            Program.objects.filter(public_visible=True)
            .filter(Q(genre=genre) | (Q(genre="") & Q(series__genre=genre)))
            .select_related("channel", "series", "asset")
            .distinct()
        )
        now = timezone.now()
        # シリーズがある番組は series_id でまとめて1枚。代表は「直近の放送」
        # (今後の放送があれば一番近い未来、無ければ一番近い過去) を採用する。
        # そのため今後分 (昇順=一番近い未来が先頭) → 過去分 (降順=一番近い過去が先頭)
        # の順に走査し、series ごとに最初に出会った1件を代表に使う。
        # シリーズ無し番組はそのまま個別カード (各放送回がそれぞれ1枚)。
        seen_series: set[int] = set()
        upcoming = base_qs.filter(start_at__gte=now).order_by("start_at")
        past = base_qs.filter(start_at__lt=now).order_by("-start_at")
        for qs in (upcoming, past):
            for p in qs[:180]:  # デdup後60件以上確保するため多めに読む
                if p.series_id:
                    if p.series_id in seen_series:
                        continue
                    seen_series.add(p.series_id)
                    card = _card(p)
                    card["series_id"] = p.series_id
                    card["title"] = p.series.title if p.series else p.title
                else:
                    card = _card(p)
                programs.append(card)
                if len(programs) >= 60:
                    break
            if len(programs) >= 60:
                break
    return {"genres": genres, "genre": genre if genre in genres else "", "programs": programs}


@router.get("/vod", response=VodOut, auth=None)
def vod(request: HttpRequest):
    from scheduling import vod as vod_mod
    from youtube.archive import recent_live_archives

    member = getattr(request, "member", None)
    vod_qs = vod_mod.available_vod_qs()
    items = [_card(p, member=member, vod=True) for p in vod_qs[:60]]
    archives = [_card(la["program"], yt=True) for la in recent_live_archives()]
    # featured Hero: 編集選択 (is_featured) のうち最新放送の VOD 公開可能なもの
    featured_p = vod_qs.filter(is_featured=True).first()
    featured = _card(featured_p, member=member, vod=True) if featured_p else None
    return {"items": items, "live_archives": archives, "featured": featured}


@router.get("/vod/{program_id}/play", response={200: VodPlayOut, 403: VodPlayOut}, auth=None)
def vod_play(request: HttpRequest, response: HttpResponse, program_id: int):
    """見逃し再生の署名 URL (#MOBILE-01)。アプリは再生直前にこれを叩く。

    SSR 版 (core.views.public_vod_detail) が HTML に URL を埋め込むのと同じゲートを通す。
    ゲート判定を素通りさせると年齢制限 (#BILL-02) とファンクラブ/サブスクの可視性が
    まとめて抜けるため、can_watch を通らない限り URL は組み立てない。

    auth=None だが匿名専用ではない: session (ブラウザ) と Bearer (アプリ) のどちらでも
    MemberAuthMiddleware が request.member を立てるので、会員限定 VOD もそのまま通る。
    未ログインは 403 + gate="login" を返し、401 にはしない (再生不可の理由を伝えるため)。
    """
    from scheduling import vod as vod_mod

    # 本文に署名 URL が乗るため SSR 版 (public_vod_detail) と同じく共有キャッシュを禁じる。
    # 403 側も可視性の判定結果を含むので、会員ごとに異なる応答をキャッシュさせない。
    response["Cache-Control"] = "no-store"

    now = timezone.now()
    # available_vod_qs を経由することで、非公開/期間外/未放送/未正規化は 404 になる
    # (SSR 版と同じ。可視性ゲート以前の「そもそも VOD として存在しない」を先に落とす)。
    program = get_object_or_404(vod_mod.available_vod_qs(now), pk=program_id)
    member = getattr(request, "member", None)
    asset = program.playback_asset

    if not vod_mod.can_watch(program, member, now):
        # 403 でも本体は同じ形。クライアントは gate で導線 (ログイン/本人確認/加入/年齢) を出し分ける。
        return Status(
            403,
            {
                "can_watch": False,
                "gate": vod_mod.gate_reason(program, member, now),
                "program_id": program.pk,
                "title": program.title,
                "duration_ms": asset.duration_ms if asset else None,
            },
        )

    url, expires_in = vod_mod.playback_url_with_expiry(program)
    return Status(
        200,
        {
            "can_watch": True,
            "gate": "",
            "url": url,
            "expires_in": expires_in,
            "program_id": program.pk,
            "title": program.title,
            "duration_ms": asset.duration_ms if asset else None,
        },
    )
