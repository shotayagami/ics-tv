# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-8): 運用 ops ダッシュボードの realtime status。staff 限定。

本丸。core.ops_views の 4 ヘルパ (_now_playing/_health/_asrun/_notifications_ctx) を集約して JSON 化し、
SPA がポーリング描画する。送出操作 (clear-slate/reload-main/slate/cm-in/cm-return/auto-return/ack) は
既存 HTMX エンドポイントを form-POST で再利用。extend/shorten/overlay の高度操作は据え置き。
"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router

from api.auth import staff_auth
from api.schemas import OpsStatusOut, TimekeeperOut

router = Router(tags=["admin"], auth=staff_auth)


def _t(dt) -> str:
    return timezone.localtime(dt).strftime("%H:%M:%S") if dt else ""


def _ev_rows(events) -> list:
    out = []
    for e in events:
        out.append(
            {
                "time": _t(e.scheduled_at),
                "action": e.action or "",
                "status": e.status or "",
                "title": e.program.title if getattr(e, "program", None) else "",
            }
        )
    return out


@router.get("/admin/ops/{slug}/status", response=OpsStatusOut)
def ops_status(request: HttpRequest, slug: str):
    from core.models import Channel
    from core.ops_views import (
        _asrun_ctx,
        _health_ctx,
        _notifications_ctx,
        _now_playing_ctx,
    )
    from medialib.models import CmBundle

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    npc = _now_playing_ctx(channel)
    hc = _health_ctx(channel)
    ac = _asrun_ctx(channel)
    nc = _notifications_ctx(channel)

    on_air = npc.get("on_air")
    on_air_out = None
    if on_air is not None:
        on_air_out = {
            "program": on_air.program.title if getattr(on_air, "program", None) else "",
            "status": on_air.status or "",
            "action": on_air.action or "",
            "note": getattr(on_air, "note", "") or "",
            "time": _t(on_air.scheduled_at),
        }
    live = npc.get("live_program")

    st = hc.get("status")
    slot = hc.get("current_slot")
    layers = []
    if st is not None and isinstance(getattr(st, "layers", None), list):
        for layer in st.layers:
            if isinstance(layer, dict):
                layers.append(
                    {
                        "layer": layer.get("layer"),
                        "role": str(layer.get("role", "")),
                        "content": str(layer.get("content", "")),
                    }
                )
    health = {
        "online": bool(hc.get("online")),
        "last_heartbeat": _t(getattr(st, "last_heartbeat_at", None)) if st else "",
        "caspar_health": (getattr(st, "caspar_health", "") or "") if st else "",
        "feed_state": (getattr(st, "feed_state", "") or "") if st else "",
        "slate_active": bool(getattr(st, "slate_active", False)) if st else False,
        "auto_return": bool(getattr(st, "auto_return", False)) if st else False,
        "auto_return_suspended": bool(getattr(st, "auto_return_suspended", False)) if st else False,
        "queue_depth": getattr(st, "queue_depth", None) if st else None,
        "last_seq": getattr(st, "last_received_seq", None) if st else None,
        "layers": layers,
        "yt_slot_status": (slot.status if slot else ""),
    }

    notifs = [
        {
            "id": n.id,
            "created": _t(n.created_at),
            "severity": getattr(n, "severity", "") or "",
            "kind": getattr(n, "kind", "") or "",
            "message": n.message,
        }
        for n in nc.get("unacked", [])
    ]

    channels = [
        {"slug": c.slug, "name": c.name}
        for c in Channel.objects.filter(enabled=True).order_by("slug")
    ]
    bundles = [{"id": b.id, "name": b.name} for b in CmBundle.objects.order_by("name")]

    # 手動速報の発火時チャイム選択肢: カテゴリ既定 (eew/weather/general) + ライブラリ音源の直接指定。
    from core.models import ChimeCategory, ChimeSound

    chime_choices = [
        {"value": f"cat:{cat}", "label": f"カテゴリ既定: {label}", "group": "category"}
        for cat, label in ChimeCategory.choices
    ]
    chime_choices += [
        {"value": f"snd:{s.id}", "label": f"ライブラリ: {s.name}", "group": "library"}
        for s in ChimeSound.objects.all()
    ]

    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "channels": channels,
        "chime_choices": chime_choices,
        "on_air": on_air_out,
        "live_program": live.title if live else "",
        "live_program_id": live.id if live else None,
        "upcoming": _ev_rows(npc.get("upcoming", [])),
        "health": health,
        "asrun": _ev_rows(ac.get("rows", [])),
        "notifications": notifs,
        "notifications_count": nc.get("unacked_count", 0),
        "bundles": bundles,
        "analytics_url": f"/ops/ch/{channel.slug}/analytics/",  # 視聴計測 (旧画面・同時接続/番組別視聴)
    }


@router.get("/admin/ops/{slug}/timekeeper", response=TimekeeperOut)
def ops_timekeeper(request: HttpRequest, slug: str):
    from core.models import Channel
    from core.ops_views import _health_ctx
    from core.timekeeper import timekeeper_ctx
    from medialib.models import CmBundle

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    ctx = timekeeper_ctx(channel)

    hc = _health_ctx(channel)
    st = hc.get("status")
    slot = hc.get("current_slot")
    layers = []
    if st is not None and isinstance(getattr(st, "layers", None), list):
        for layer in st.layers:
            if isinstance(layer, dict):
                layers.append(
                    {
                        "layer": layer.get("layer"),
                        "role": str(layer.get("role", "")),
                        "content": str(layer.get("content", "")),
                    }
                )
    ctx["health"] = {
        "online": bool(hc.get("online")),
        "last_heartbeat": _t(getattr(st, "last_heartbeat_at", None)) if st else "",
        "caspar_health": (getattr(st, "caspar_health", "") or "") if st else "",
        "feed_state": (getattr(st, "feed_state", "") or "") if st else "",
        "slate_active": bool(getattr(st, "slate_active", False)) if st else False,
        "auto_return": bool(getattr(st, "auto_return", False)) if st else False,
        "auto_return_suspended": bool(getattr(st, "auto_return_suspended", False)) if st else False,
        "queue_depth": getattr(st, "queue_depth", None) if st else None,
        "last_seq": getattr(st, "last_received_seq", None) if st else None,
        "layers": layers,
        "yt_slot_status": (slot.status if slot else ""),
    }
    ctx["channel"] = {"slug": channel.slug, "name": channel.name}
    ctx["channels"] = [
        {"slug": c.slug, "name": c.name}
        for c in Channel.objects.filter(enabled=True).order_by("slug")
    ]
    ctx["bundles"] = [{"id": b.id, "name": b.name} for b in CmBundle.objects.order_by("name")]
    ctx["live_program_id"] = (
        ctx["broadcast"]["program_id"] if ctx["broadcast"]["program_type"] == "live" else None
    )
    return ctx
