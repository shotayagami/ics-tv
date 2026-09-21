# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開プレイヤー島向け API (#Phase1)。

チャンネルタブ (GET /channels) と チャンネル詳細 (GET /channels/{slug}: 現在/次番組・本日の編成・
再生URL・YouTube fallback)。既存 view 層 (core.views ヘルパ / core.epg / core.now_playing) を
薄く JSON 化する (strangler-fig)。閲覧は公開 (auth=None)。会員がいれば pin 状態を反映する。
"""

from datetime import timedelta

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router

from api.schemas import ChannelDetail, ChannelTab

router = Router(tags=["player"])


def _program_now(p) -> dict | None:
    if p is None:
        return None
    return {
        "id": p.id,
        "title": p.title,
        "start_ts": int(p.start_at.timestamp()),
        "end_ts": int(p.end_at.timestamp()),
        "genre": p.resolved_genre or "",
        "is_rerun": False,
        "series_url": p.series.public_url if p.series_id else None,
    }


def _rerun_now(info) -> dict | None:
    """編成 Program が無い時間帯でも、再放送フィラー (rerun_eligible) なら現在番組として露出。

    id=0 は編成 Program ではない合成行 (番組詳細リンク無し)。進行バーは送出区間で算出する。
    """
    if not info or not info["is_rerun"] or not info["start"] or not info["end"]:
        return None
    return {
        "id": 0,
        "title": info["title"],
        "start_ts": int(info["start"].timestamp()),
        "end_ts": int(info["end"].timestamp()),
        "genre": "",
        "is_rerun": True,
    }


@router.get("/channels", response=list[ChannelTab], auth=None)
def channels(request: HttpRequest):
    from core import now_playing
    from core.models import Channel
    from core.views import _member_pinned_channel_ids

    now = timezone.now()
    member = getattr(request, "member", None)
    pinned = _member_pinned_channel_ids(member)
    chs = list(Channel.objects.filter(enabled=True).order_by("slug"))
    if pinned:
        chs.sort(key=lambda c: c.id not in pinned)  # 安定ソートで slug 順は維持
    return [
        {
            "slug": c.slug,
            "name": c.name,
            "short": c.short_name,
            "tint": c.tint_color,
            "live": now_playing.is_online(c, now) and not now_playing.is_broadcast_paused(c, now),
            "pinned": c.id in pinned,
        }
        for c in chs
    ]


@router.get("/channels/{slug}", response=ChannelDetail, auth=None)
def channel_detail(request: HttpRequest, response: HttpResponse, slug: str):
    from core import epg as epg_mod
    from core import now_playing
    from core.models import Channel
    from core.views import _local_day_bounds, _member_pinned_channel_ids, _public_programs
    from scheduling.exposure_gate import can_watch_live, gate_reason, hls_url_for
    from scheduling.resolver import project_filler_segments
    from youtube.models import YoutubeSlot, YtSlotStatus

    # 署名付き再生URL・ゲート理由は視聴者ごとに異なるため、共有/CDNキャッシュに載せない
    # (#27 Phase B のレビューで指摘: live_poster_serve には既にこの対策があったが、この
    # エンドポイント自体には無く、CDNが会員ごとに異なるべき応答をURL単位でキャッシュ
    # すると他の視聴者へそのまま配信してしまう=ゲートが意味を失う)。
    response["Cache-Control"] = "private, no-store"

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = timezone.now()
    today = timezone.localdate()
    ts, te = _local_day_bounds(today)
    todays = _public_programs(channel, ts, te + timedelta(days=1))  # 日跨ぎ番組も拾う
    reruns = project_filler_segments(channel, now, te + timedelta(days=1))  # これから分の再放送帯
    current = next((p for p in todays if p.start_at <= now < p.end_at), None)
    upcoming = next((p for p in todays if p.start_at > now), None)
    # 編成 Program が現在無くても、再放送フィラー送出中なら「現在番組」として露出する。
    current_now = _program_now(current) or _rerun_now(now_playing.on_air_info(channel, now))
    member = getattr(request, "member", None)
    pinned = _member_pinned_channel_ids(member)

    slot = YoutubeSlot.objects.filter(
        channel=channel, window_start__lte=now, window_end__gt=now, status=YtSlotStatus.LIVE
    ).first()

    # exposure_policy (#27) + ファンクラブ ティア軸 (#27 Phase B): 現在番組(編成 Program)が
    # site_members/members_yt_site ならサイト会員限定、fc_required_level が設定されていれば
    # そのティア以上限定 (docs/site-only-broadcast.md §4.7・docs/fanclub.md §6.4)。編成 Program が
    # 無い (再放送フィラー中) は常に完全公開扱い (can_watch_live(None, member) == True)。
    entitled = can_watch_live(current, member)

    # 意図的な放送休止中は HLS を出さず (公開プレイヤーがスレートを流し続けないよう) 休止表示に倒す。
    paused = now_playing.is_broadcast_paused(channel, now)
    return {
        "slug": channel.slug,
        "name": channel.name,
        "short": channel.short_name,
        "tint": channel.tint_color,
        "live": now_playing.is_online(channel, now) and not paused,
        "paused": paused,
        "next_on_air": now_playing.next_on_air_label(channel, now) if paused else None,
        "is_pinned": channel.id in pinned,
        "hls_url": None if (paused or not entitled) else hls_url_for(channel, current, member),
        "gate_reason": "" if (paused or entitled) else gate_reason(current, member),
        # YouTube rolling 枠フォールバックも本線同様に共有 Live Input を映すため、entitled でない
        # ときはこちらも渡さない (渡すと VideoPlayer.tsx の hlsUrl→youtubeBroadcastId→placeholder
        # のフォールバック順で iframe 埋め込みが先に来てしまい、site-member/ファンクラブ ティア
        # 軸のゲートが素通りする。#27 Phase B レビューで発見)。
        "youtube_broadcast_id": slot.broadcast_id if (slot and entitled) else None,
        "current": current_now,
        "upcoming": _program_now(upcoming),
        "day_list": epg_mod.day_list(channel, todays, now, rerun_segments=reruns),
    }


@router.get("/hls-auth", auth=None)
def hls_auth(request: HttpRequest, channel: str = "", token: str = ""):
    """本線 HLS エッジ認証 (#27、docs/site-only-broadcast.md §4.7・リスク#3)。

    送出ノード側のエッジ (nginx auth_request 等。deploy/ 側の配線は別途・本番未適用) が
    再生 URL のクエリ (?token=...) をこのエンドポイントで検証する想定。channel は
    Channel.slug (署名時と同じ値)。200=許可、403=拒否 ({"detail": ...} は ninja 既定)。
    """
    from ninja.errors import HttpError

    from core.hls_auth import verify_hls_token

    if not (channel and token and verify_hls_token(channel, token)):
        raise HttpError(403, "invalid or expired token")
    return {"ok": True}
