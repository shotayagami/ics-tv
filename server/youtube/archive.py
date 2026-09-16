# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""live番組の見逃し = YouTube アーカイブ解決 (#VOD-01 live)。

放送済み live 番組の時間帯に重なる COMPLETE 枠 (broadcast_id 付き) を見つけ、YouTube の
watch URL を返す。privacy=private のチャンネルは出さない。録画番組の asset VOD (scheduling.vod)
とは別経路 (live は asset を持たないため)。Program↔Slot の明示 FK は無く時間窓で対応付ける。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from scheduling.models import Program, ProgramType
from youtube.models import (
    ProgramBroadcast,
    YoutubeConfig,
    YoutubeSlot,
    YtPrivacy,
    YtSlotStatus,
)


def _privacy_ok(channel_id: int) -> bool:
    cfg = YoutubeConfig.objects.filter(channel_id=channel_id).first()
    return bool(cfg and cfg.privacy != YtPrivacy.PRIVATE)


def dedicated_broadcast_for_program(program: Program) -> ProgramBroadcast | None:
    """program の専用枠 (#23, COMPLETE かつ broadcast_id 付き)。録画/生どちらでも対象。無ければ None。"""
    return (
        ProgramBroadcast.objects.filter(
            program=program,
            status=YtSlotStatus.COMPLETE,
            broadcast_id__isnull=False,
        )
        .exclude(broadcast_id="")
        .select_related("preset")
        .first()
    )


def slot_for_program(program: Program) -> YoutubeSlot | None:
    """program 開始時刻を内包する COMPLETE 枠 (broadcast_id 付き)。無ければ None。

    2h 枠は番組と 1:1 でないため、番組開始を含む枠で対応付ける。
    """
    return (
        YoutubeSlot.objects.filter(
            channel_id=program.channel_id,
            status=YtSlotStatus.COMPLETE,
            broadcast_id__isnull=False,
            window_start__lte=program.start_at,
            window_end__gt=program.start_at,
        )
        .exclude(broadcast_id="")
        .order_by("window_start")
        .first()
    )


def archive_watch_url(program: Program) -> str:
    """番組の見逃し用 YouTube watch URL。対象外/未アーカイブ/非公開は空文字。

    #23 専用枠があれば rolling 枠より優先する (番組単位のクリーンなアーカイブに向ける)。専用枠は
    録画/生どちらでも対象で、公開可否は preset.privacy で判定。専用枠が無い場合は従来どおり
    live 番組を rolling 枠 (時間窓対応) で解決する。
    """
    pb = dedicated_broadcast_for_program(program)
    if pb:
        preset = pb.preset
        if preset is None or preset.privacy != YtPrivacy.PRIVATE:
            return f"https://www.youtube.com/watch?v={pb.broadcast_id}"
        return ""  # 専用枠が private 指定 → 露出しない (rolling へはフォールバックしない)
    if program.type != ProgramType.LIVE or not _privacy_ok(program.channel_id):
        return ""
    slot = slot_for_program(program)
    return f"https://www.youtube.com/watch?v={slot.broadcast_id}" if slot else ""


def recent_live_archives(now=None, *, days: int = 14, limit: int = 30) -> list[dict]:
    """直近に放送済みで YouTube アーカイブが見られる live 番組 [{program, url}] (新しい順)。"""
    now = now or timezone.now()
    since = now - timedelta(days=days)
    progs = (
        Program.objects.filter(
            type=ProgramType.LIVE,
            public_visible=True,
            end_at__lte=now,
            end_at__gte=since,
        )
        .select_related("channel", "series")
        .order_by("-end_at")[: limit * 2]
    )
    out: list[dict] = []
    for p in progs:
        url = archive_watch_url(p)
        if url:
            out.append({"program": p, "url": url})
        if len(out) >= limit:
            break
    return out
