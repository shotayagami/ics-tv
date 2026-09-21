# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-3): 編成タイムラインの admin API。staff 限定。

最難の本丸スライス。読み取り (グリッド描画用の番組ジオメトリ) はここで JSON 化し、ドラッグ移動/
リサイズの更新は既存の JSON エンドポイント (scheduling.edit_views.program_move/resize) を SPA から
そのまま叩く (favorite トグル等と同じ再利用方針)。view 層 scheduling.views.timeline と同じデータ。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, time, timedelta

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Query, Router

from api.auth import staff_auth
from api.schemas import SchedChannel, TimelineOut, WeekOut

router = Router(tags=["admin"], auth=staff_auth)


def _cue_asset_ids(programs) -> set[int]:
    from medialib.models import CueSheet

    return set(
        CueSheet.objects.filter(
            asset_id__in=[p.asset_id for p in programs if p.asset_id]
        ).values_list("asset_id", flat=True)
    )


def _program_geo(p, cue_asset_ids: set[int]) -> dict:
    """Program 1件をグリッド描画用ジオメトリ dict へ (timeline / week 共用)。"""
    asset = p.asset
    src = asset.title if asset else (p.live_source.name if p.live_source else "")
    return {
        "id": p.id,
        "type": p.type,
        "title": p.title,
        "start_at": p.start_at.isoformat(),
        "end_at": p.end_at.isoformat(),
        "public_visible": p.public_visible,
        "source": src,
        "thumb": p.thumb_url or "",
        "asset_duration_ms": asset.duration_ms if asset else None,
        "has_cuesheet": p.asset_id in cue_asset_ids,
        "breaks": [
            {
                "id": b.id,
                "offset_ms": b.offset_ms,
                "grid": b.grid,
                "duration_ms": b.duration_ms,
            }
            for b in sorted(p.ad_breaks.all(), key=lambda b: b.offset_ms)
        ],
    }


@router.get("/admin/scheduling/channels", response=list[SchedChannel])
def scheduling_channels(request: HttpRequest):
    from core.models import Channel

    return [{"slug": c.slug, "name": c.name} for c in Channel.objects.order_by("slug")]


@router.get("/admin/scheduling/{slug}/timeline", response=TimelineOut)
def timeline(request: HttpRequest, slug: str):
    from core.models import Channel
    from scheduling.models import Program

    channel = get_object_or_404(Channel, slug=slug)
    now = timezone.now()
    horizon = now + timedelta(hours=24)
    programs = list(
        Program.objects.filter(
            channel=channel,
            end_at__gt=now - timedelta(hours=1),
            start_at__lt=horizon,
        )
        .select_related("asset", "live_source", "series")
        .prefetch_related("ad_breaks")
        .order_by("start_at")
    )
    cue_asset_ids = _cue_asset_ids(programs)
    programs_data = [_program_geo(p, cue_asset_ids) for p in programs]
    channels = [{"slug": c.slug, "name": c.name} for c in Channel.objects.order_by("slug")]
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "now": now.isoformat(),
        "horizon": horizon.isoformat(),
        "programs": programs_data,
        "channels": channels,
    }


def _slot_source(slot) -> str:
    if slot.program_type == "live":
        return slot.live_source.name if slot.live_source else ""
    return slot.default_asset.title if slot.default_asset else ""


@router.get("/admin/scheduling/{slug}/week", response=WeekOut)
def week(request: HttpRequest, slug: str, start: str = Query("")):
    """週間グリッド: 当週 (月〜日) の実 Program + 基本編成スロットの投影。

    実 Program は曜日列ジオメトリ (差分レイヤ)、SeriesSlot は各日付へ matches() 投影 (下地レイヤ)。
    タイムゾーン: 列割当/時間軸は localtime、スロット投影開始は make_aware(combine()) で
    expand_series_slots と一致させる (DST 差異を排除。JST は DST 無)。
    """
    from core.models import Channel
    from scheduling.models import Program, SeriesSlot

    channel = get_object_or_404(Channel, slug=slug)

    # start 指定が無ければ今週月曜。指定時も月曜にスナップ (グリッドは常に月〜日)。
    base = timezone.localdate()
    if start:
        with contextlib.suppress(ValueError):
            base = datetime.strptime(start, "%Y-%m-%d").date()
    week_start = base - timedelta(days=base.weekday())
    days = [week_start + timedelta(days=i) for i in range(7)]

    def _aware(d, t: time):
        return timezone.make_aware(datetime.combine(d, t))

    win_start = _aware(days[0], time.min)
    win_end = _aware(days[6] + timedelta(days=1), time.min)

    programs = list(
        Program.objects.filter(
            channel=channel,
            end_at__gt=win_start,
            start_at__lt=win_end,
        )
        .select_related("asset", "live_source", "series")
        .prefetch_related("ad_breaks")
        .order_by("start_at")
    )
    cue_asset_ids = _cue_asset_ids(programs)
    programs_data = []
    # 実 Program の (series_id, 開始 localtime) 集合 → スロット投影の covered 判定に使う。
    covered_keys: set[tuple[int, str]] = set()
    for p in programs:
        local = timezone.localtime(p.start_at)
        geo = _program_geo(p, cue_asset_ids)
        # 窓内 7 日に収まる列のみ (前週からの食い込みは start_at の曜日で素直に割当)。
        dow = (local.date() - week_start).days
        geo["dow"] = max(0, min(6, dow))
        programs_data.append(geo)
        if p.series_id:
            covered_keys.add((p.series_id, f"{local.date().isoformat()} {local.strftime('%H:%M')}"))

    slots = (
        SeriesSlot.objects.filter(
            series__channel=channel,
            series__is_active=True,
            effective_from__lte=days[6],
        )
        .select_related("series", "default_asset", "live_source")
        .order_by("start_time")
    )
    slot_occ = []
    for sl in slots:
        label = sl.recurrence_label()
        src = _slot_source(sl)
        hhmm = sl.start_time.strftime("%H:%M")
        for i, d in enumerate(days):
            if d < sl.effective_from:
                continue
            if sl.effective_to is not None and d > sl.effective_to:
                continue
            if not sl.matches(d):
                continue
            covered = (sl.series_id, f"{d.isoformat()} {hhmm}") in covered_keys
            slot_occ.append(
                {
                    "slot_id": sl.id,
                    "series_id": sl.series_id,
                    "series_title": sl.series.title,
                    "dow": i,
                    "date": d.isoformat(),
                    "start_time": hhmm,
                    "duration_ms": sl.duration_ms,
                    "program_type": sl.program_type,
                    "source": src,
                    "recurrence_kind": sl.recurrence_kind,
                    "recurrence_label": label,
                    "covered": covered,
                }
            )

    channels = [{"slug": c.slug, "name": c.name} for c in Channel.objects.order_by("slug")]
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "week_start": week_start.isoformat(),
        "days": [d.isoformat() for d in days],
        "programs": programs_data,
        "slots": slot_occ,
        "channels": channels,
    }
