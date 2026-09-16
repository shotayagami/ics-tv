# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""営業 UI (#6 / ui-sales.md §3 線引きエディタ・§4 割付ビュー)。staff (営業/管理者) 専用。

線引き = spot_order_band の CRUD + spot_order_program (指定番組)。割付ビューは日別 ad_break×placement
の read-only 表示 + refill トリガ + 手動差し替え (未送出 SCHEDULED の素材のみ差替、帰属は保持)。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Channel
from medialib.models import CmCreative
from playout.models import PlayoutEvent, PlayoutStatus
from sales.models import (
    Placement,
    PlacementMatch,
    SpotOrder,
    SpotOrderBand,
    SpotOrderProgram,
)
from scheduling.models import AdBreakItem, Program, Series

_UNSENT = (PlayoutStatus.SCHEDULED, PlayoutStatus.EXECUTING)


def _ok(message: str, trigger: str) -> HttpResponse:
    return HttpResponse(message, headers={"HX-Trigger": trigger})


# ---- 線引きエディタ (§3) ----


@staff_member_required
def band_editor(request, spot_order_id: int) -> HttpResponse:
    so = get_object_or_404(
        SpotOrder.objects.select_related("contract", "channel"), pk=spot_order_id
    )
    dows = [(0, "月"), (1, "火"), (2, "水"), (3, "木"), (4, "金"), (5, "土"), (6, "日")]
    band_rows = [
        {"b": b, "days": "".join(label for v, label in dows if b.dow_mask & (1 << v))}
        for b in so.bands.order_by("dow_mask", "start_time")
    ]
    return render(
        request,
        "sales/band_editor.html",
        {
            "so": so,
            "band_rows": band_rows,
            "programs": so.programs.select_related("series"),
            "series_options": Series.objects.filter(channel_id=so.channel_id, is_active=True),
            "dows": dows,
        },
    )


@staff_member_required
@require_POST
def op_add_band(request, spot_order_id: int) -> HttpResponse:
    so = get_object_or_404(SpotOrder, pk=spot_order_id)
    dows = request.POST.getlist("dow")
    start, end = request.POST.get("start_time"), request.POST.get("end_time")
    if not dows or not start or not end:
        return HttpResponse("曜日・開始・終了は必須です", status=400)
    mask = 0
    for d in dows:
        mask |= 1 << int(d)
    if start >= end:
        return HttpResponse("開始 < 終了 が必要です (日跨ぎは 2 行に分割)", status=400)
    SpotOrderBand.objects.create(spot_order=so, dow_mask=mask, start_time=start, end_time=end)
    return _ok("線引きを追加しました", "bandChanged")


@staff_member_required
@require_POST
def op_delete_band(request, band_id: int) -> HttpResponse:
    band = get_object_or_404(SpotOrderBand, pk=band_id)
    band.delete()
    return _ok("線引きを削除しました", "bandChanged")


@staff_member_required
@require_POST
def op_add_program(request, spot_order_id: int) -> HttpResponse:
    so = get_object_or_404(SpotOrder, pk=spot_order_id)
    series_id = request.POST.get("series_id")
    series = Series.objects.filter(pk=series_id, channel_id=so.channel_id).first()
    if series is None:
        return HttpResponse("series が不正です (同一 channel のみ)", status=400)
    SpotOrderProgram.objects.get_or_create(spot_order=so, series=series)
    return _ok("指定番組を追加しました", "bandChanged")


@staff_member_required
@require_POST
def op_delete_program(request, sop_id: int) -> HttpResponse:
    get_object_or_404(SpotOrderProgram, pk=sop_id).delete()
    return _ok("指定番組を削除しました", "bandChanged")


# ---- 割付ビュー (§4) ----


def _aware_day(d: str):
    parsed = datetime.strptime(d, "%Y-%m-%d")
    start = timezone.make_aware(datetime.combine(parsed.date(), time.min))
    return start, start + timedelta(days=1)


@staff_member_required
def allocation_view(request) -> HttpResponse:
    channel_id = request.GET.get("channel")
    day = request.GET.get("date") or timezone.now().date().isoformat()
    channels = list(Channel.objects.order_by("id"))
    channel = (
        Channel.objects.filter(pk=channel_id).first()
        if channel_id
        else (channels[0] if channels else None)
    )
    rows: list = []
    if channel is not None:
        start, end = _aware_day(day)
        programs = (
            Program.objects.filter(channel=channel, start_at__lt=end, end_at__gt=start)
            .order_by("start_at")
            .prefetch_related("ad_breaks__items__cm_asset__asset")
        )
        # placement / 未送出 status を一括引き
        item_ids = [it.id for p in programs for br in p.ad_breaks.all() for it in br.items.all()]
        pl_map = {
            pl.ad_break_item_id: pl
            for pl in Placement.objects.filter(ad_break_item_id__in=item_ids)
        }
        unsent = set(
            PlayoutEvent.objects.filter(
                ad_break_item_id__in=item_ids, status__in=_UNSENT
            ).values_list("ad_break_item_id", flat=True)
        )
        for p in programs:
            breaks = []
            for br in p.ad_breaks.all():
                items = []
                for it in br.items.all():
                    pl = pl_map.get(it.id)
                    items.append(
                        {
                            "item": it,
                            "cm": it.cm_asset,
                            "match": pl.match_kind if pl else None,
                            "editable": it.id in unsent,
                        }
                    )
                breaks.append({"br": br, "items": items})
            rows.append({"program": p, "breaks": breaks})
    return render(
        request,
        "sales/allocation.html",
        {
            "channels": channels,
            "channel": channel,
            "current_ch_slug": channel.slug if channel else "",  # base.html nav の timeline url 用
            "day": day,
            "rows": rows,
            "matches": PlacementMatch.choices,
        },
    )


@staff_member_required
@require_POST
def op_swap_item(request, item_id: int) -> HttpResponse:
    """手動差し替え (#6 Phase C)。未送出 (SCHEDULED) の枠の素材のみ差替。契約帰属は保持。"""
    item = get_object_or_404(AdBreakItem.objects.select_related("ad_break"), pk=item_id)
    events = list(PlayoutEvent.objects.filter(ad_break_item_id=item.id))
    if any(ev.status != PlayoutStatus.SCHEDULED for ev in events):
        return HttpResponse("送出済/実行中の枠は差し替えできません (S11 不変)", status=409)
    new_cm = (
        CmCreative.objects.select_related("asset")
        .filter(pk=request.POST.get("cm_asset_id"))
        .first()
    )
    if new_cm is None:
        return HttpResponse("差替先 CM が見つかりません", status=400)
    if new_cm.grid != item.ad_break.grid:
        return HttpResponse("grid が枠と一致しません", status=400)
    with transaction.atomic():
        item.cm_asset = new_cm
        item.save(update_fields=["cm_asset"])
        # 紐づく SCHEDULED イベントの素材参照も同期 (差替の即時反映)
        for ev in events:
            ev.asset_id = new_cm.asset_id
            ev.params = {
                **ev.params,
                "clip": f"cm/{new_cm.asset_id}",
                "advertiser": new_cm.advertiser,
                "duration_ms": new_cm.asset.duration_ms or 0,
            }
            ev.save(update_fields=["asset", "params"])
    return _ok("素材を差し替えました", "itemSwapped")


@staff_member_required
@require_POST
def op_refill(request) -> HttpResponse:
    """契約/考査変更の反映トリガ (refill_window 同期実行)。未送出枠のみ再充填 (S11)。"""
    from sales.tasks import refill_window

    channel_id = request.POST.get("channel")
    channel = Channel.objects.filter(pk=channel_id).first()
    if channel is None:
        return HttpResponse("channel が不正です", status=400)
    res = refill_window(channel.id)
    return _ok(f"再充填しました (clear {res['cleared']} 枠)", "refilled")
