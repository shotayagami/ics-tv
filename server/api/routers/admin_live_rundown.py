# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA: 生キューシート (LiveRundown/LiveCue)。staff 限定 (docs/timekeeper-live.md §9)。"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router

from api.auth import staff_auth
from api.schemas import LiveRundownOut, RundownTemplateOut

router = Router(tags=["admin"], auth=staff_auth)


@router.get("/admin/scheduling/{slug}/live-rundown/{int:program_id}", response=LiveRundownOut)
def live_rundown(request: HttpRequest, slug: str, program_id: int):
    from core.models import Channel
    from medialib.models import Asset, CmBundle, NormalizeStatus
    from scheduling.models import LiveRundown, Program

    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(Program, pk=program_id, channel=channel)
    rundown = LiveRundown.objects.filter(program=prog).first()
    cues = (
        list(rundown.cues.select_related("cm_bundle", "asset").order_by("seq")) if rundown else []
    )

    rows, offset_ms = [], 0
    for c in cues:
        rows.append(
            {
                "id": c.id,
                "seq": c.seq,
                "kind": c.kind,
                "kind_label": c.get_kind_display(),
                "label": c.label or "",
                "planned_duration_ms": c.planned_duration_ms,
                "planned_at": int(prog.start_at.timestamp()) + offset_ms // 1000,
                "cm_bundle_id": c.cm_bundle_id,
                "cm_bundle_name": c.cm_bundle.name if c.cm_bundle else "",
                "grid": c.grid or "",
                "asset_id": c.asset_id,
                "asset_title": c.asset.title if c.asset else "",
                "state": c.state,
                "state_label": c.get_state_display(),
                "auto_fire": c.auto_fire,
                "auto_offset_ms": c.auto_offset_ms,
                "auto_anchor": c.auto_anchor,
                "auto_wall_time": c.auto_wall_time.strftime("%H:%M") if c.auto_wall_time else "",
            }
        )
        offset_ms += c.planned_duration_ms

    planned_total_ms = sum(c.planned_duration_ms for c in cues)
    slot_ms = int((prog.end_at - prog.start_at).total_seconds() * 1000)
    return {
        "program_id": prog.id,
        "program_title": prog.title,
        "channel": {"slug": channel.slug, "name": channel.name},
        "program_start_at": int(prog.start_at.timestamp()),
        "program_end_at": int(prog.end_at.timestamp()),
        "cues": rows,
        "planned_total_ms": planned_total_ms,
        "over_under_ms": planned_total_ms - slot_ms,
        "bundles": [{"id": b.id, "name": b.name} for b in CmBundle.objects.order_by("name")],
        "assets": [
            {"id": a.id, "name": a.title}
            for a in Asset.objects.filter(
                usable_as_program=True, normalize_status=NormalizeStatus.READY
            ).order_by("title")
        ],
    }


@router.get("/admin/scheduling/{slug}/rundown-template/{int:slot_id}", response=RundownTemplateOut)
def rundown_template(request: HttpRequest, slug: str, slot_id: int):
    """SeriesSlot の定番進行表(雛形)。live_rundown と同じ shape を返し、共有エディタで扱う。"""
    from core.models import Channel
    from medialib.models import Asset, CmBundle, NormalizeStatus
    from scheduling.models import LiveRundownTemplate, SeriesSlot

    channel = get_object_or_404(Channel, slug=slug)
    slot = get_object_or_404(
        SeriesSlot.objects.select_related("series"), pk=slot_id, series__channel=channel
    )
    template = LiveRundownTemplate.objects.filter(slot=slot).first()
    cues = (
        list(template.cues.select_related("cm_bundle", "asset").order_by("seq")) if template else []
    )

    rows, offset_ms = [], 0
    for c in cues:
        rows.append(
            {
                "id": c.id,
                "seq": c.seq,
                "kind": c.kind,
                "kind_label": c.get_kind_display(),
                "label": c.label or "",
                "planned_duration_ms": c.planned_duration_ms,
                # 雛形は絶対時刻を持たない → スロット先頭からの相対秒 (表示用の目安)。
                "planned_at": offset_ms // 1000,
                "cm_bundle_id": c.cm_bundle_id,
                "cm_bundle_name": c.cm_bundle.name if c.cm_bundle else "",
                "grid": c.grid or "",
                "asset_id": c.asset_id,
                "asset_title": c.asset.title if c.asset else "",
                # 雛形は state を持たない (常に編集可能) → 行型互換のため固定値。
                "state": "pending",
                "state_label": "",
                "auto_fire": c.auto_fire,
                "auto_offset_ms": c.auto_offset_ms,
                "auto_anchor": c.auto_anchor,
                "auto_wall_time": c.auto_wall_time.strftime("%H:%M") if c.auto_wall_time else "",
            }
        )
        offset_ms += c.planned_duration_ms

    planned_total_ms = sum(c.planned_duration_ms for c in cues)
    slot_label = (
        f"{slot.recurrence_label()} {slot.start_time.strftime('%H:%M')} {slot.series.title}"
    )
    return {
        "slot_id": slot.id,
        "slot_label": slot_label,
        "channel": {"slug": channel.slug, "name": channel.name},
        "slot_duration_ms": slot.duration_ms,
        "cues": rows,
        "planned_total_ms": planned_total_ms,
        "over_under_ms": planned_total_ms - slot.duration_ms,
        "bundles": [{"id": b.id, "name": b.name} for b in CmBundle.objects.order_by("name")],
        "assets": [
            {"id": a.id, "name": a.title}
            for a in Asset.objects.filter(
                usable_as_program=True, normalize_status=NormalizeStatus.READY
            ).order_by("title")
        ],
    }
