# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-9): 営業 CM割付 sales の allocation 概観。staff 限定。

日付/チャンネル単位で 番組 → CM枠(ad_break) → 配置 CM(placement) を読み取り JSON 化。自動補充
(refill)・CM 入替(swap, #2e-3) は既存 sales エンドポイントを form-POST 再利用。入替候補 cm_options は
考査OK の CmCreative を grid 付きで返し、SPA 側で枠 grid に一致するものだけ提示。線引き(band)エディタ
はドラッグ中心で複雑なため据え置き、edit_url で旧 allocation 画面へ。既存 allocation_view と同等。
"""

from __future__ import annotations

from datetime import datetime, time

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router

from api.auth import staff_auth
from api.schemas import AllocationOut

router = Router(tags=["admin"], auth=staff_auth)


def _ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


@router.get("/admin/sales/allocation", response=AllocationOut)
def allocation(request: HttpRequest, channel: int | None = Query(None), date: str = Query("")):
    from core.models import Channel
    from playout.models import PlayoutEvent, PlayoutStatus
    from sales.models import Placement
    from scheduling.models import Program

    channels = list(Channel.objects.order_by("id"))
    day = date or timezone.localdate().isoformat()  # ローカル日付 (旧 view の UTC date より正確)
    ch = next((c for c in channels if c.id == channel), None) or (channels[0] if channels else None)

    rows = []
    if ch is not None:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(d, time.min), tz)
        end = timezone.make_aware(datetime.combine(d, time.max), tz)
        programs = (
            Program.objects.filter(channel=ch, start_at__lt=end, end_at__gt=start)
            .order_by("start_at")
            .prefetch_related("ad_breaks__items__cm_asset__asset")
        )
        item_ids = [it.id for p in programs for br in p.ad_breaks.all() for it in br.items.all()]
        pl_map = {
            pl.ad_break_item_id: pl
            for pl in Placement.objects.filter(ad_break_item_id__in=item_ids)
        }
        # 入替可否は op_swap_item と同一規則: SCHEDULED 以外のイベント(送出済/実行中)があれば不可。
        # イベント未生成の枠は入替可 (op_swap_item は events 空なら通す)。
        locked = set(
            PlayoutEvent.objects.filter(ad_break_item_id__in=item_ids)
            .exclude(status=PlayoutStatus.SCHEDULED)
            .values_list("ad_break_item_id", flat=True)
        )
        for p in programs:
            breaks = []
            for br in sorted(p.ad_breaks.all(), key=lambda b: b.offset_ms):
                items = []
                used = 0
                for it in br.items.all():
                    pl = pl_map.get(it.id)
                    if it.cm_asset_id:
                        used += it.cm_asset.asset.duration_ms or 0
                    items.append(
                        {
                            "item_id": it.id,
                            "advertiser": it.cm_asset.advertiser if it.cm_asset_id else "",
                            "match": (pl.match_kind if pl else "") or "",
                            "editable": it.id not in locked,
                        }
                    )
                unit = 15000 if br.grid == "15s" else 20000
                remaining_ms = max(0, br.duration_ms - used)
                breaks.append(
                    {
                        "break_id": br.id,
                        "offset": _ms(br.offset_ms),
                        "grid": br.grid,
                        "items": items,
                        "remaining": _ms(remaining_ms),
                        "full": remaining_ms < unit,
                    }
                )
            rows.append(
                {
                    "title": p.title,
                    "time": timezone.localtime(p.start_at).strftime("%H:%M"),
                    "breaks": breaks,
                }
            )

    from medialib.models import CmCreative, ScreeningStatus

    cm_options = [
        {"id": c.asset_id, "name": c.advertiser, "grid": c.grid}
        for c in CmCreative.objects.filter(screening_status=ScreeningStatus.APPROVED)
        .select_related("asset")
        .order_by("advertiser")[:300]
    ]
    return {
        "channels": [{"id": c.id, "name": c.name} for c in channels],
        "channel_id": ch.id if ch else None,
        "channel_name": ch.name if ch else "",
        "day": day,
        "rows": rows,
        "cm_options": cm_options,
        "edit_url": "/sales/allocation/",
    }
