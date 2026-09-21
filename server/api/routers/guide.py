# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開番組表グリッド API (#Phase2)。

時間軸×チャンネルのタイムライングリッドを構造化 JSON で返す (core.epg.build_epg を再利用)。
ブロックの style 文字列は使わず、座標/色を構造化フィールドで返し、React 島が style を組む。
?date=YYYY-MM-DD で日指定 (既定=今日)。閲覧は公開 (auth=None)。
"""

from datetime import timedelta

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from api.schemas import GuideOut

router = Router(tags=["guide"])

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


@router.get("/guide", response=GuideOut, auth=None)
def guide(request: HttpRequest, date: str = ""):
    from core import epg as epg_mod
    from core.models import Channel
    from core.views import _local_day_bounds, _parse_date, _public_programs
    from scheduling.resolver import project_filler_segments

    now = timezone.now()
    today = timezone.localdate()
    base = _parse_date(date, today)
    ds, de = _local_day_bounds(base)
    columns = [
        {
            "channel": ch,
            "programs": _public_programs(ch, ds, de),
            # 編成に無いフィラー帯を再放送 (rerun_eligible 素材) として埋める (表示専用投影)。
            "rerun_segments": project_filler_segments(ch, ds, de),
        }
        for ch in Channel.objects.filter(enabled=True).order_by("slug")
    ]
    epg = epg_mod.build_epg(columns, base, now)
    return {
        "base_date": base.isoformat(),
        "label": f"{base:%Y-%m-%d} ({_WEEKDAY_JA[base.weekday()]})",
        "is_today": base == today,
        "prev_date": (base - timedelta(days=1)).isoformat(),
        "next_date": (base + timedelta(days=1)).isoformat(),
        "today": today.isoformat(),
        "height": epg["height"],
        "px_per_min": epg["px_per_min"],
        "now_top": epg["now_top"],
        "now_min": epg["now_min"],
        "now_label": epg["now_label"],
        "show_now": epg["show_now"],
        "hours": [{"label": h["label"], "top": h["top"]} for h in epg["hours"]],
        # build_epg のブロック dict は GuideBlock の全フィールド (+ 余分な style) を持つ。
        # ninja は余剰キーを無視するのでそのまま渡す。
        "cols": [
            {
                "slug": c["slug"],
                "name": c["name"],
                "short": c["short"],
                "tint": c["tint"],
                "blocks": c["blocks"],
            }
            for c in epg["cols"]
        ],
    }
