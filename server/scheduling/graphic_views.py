# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""自動グラフィックセット (GraphicCue) の専用編集 UI (#18 §C / docs/cg-layers.md)。

owner = series / program / filler。
- series  : 繰り返し原本。expand_series_slots が各回 Program へコピーする。
- program : この回のみ (Series からコピー済を個別編集 or 単発番組)。
- filler  : Channel 基本セット (context="filler")。フィラー全クリップに常時適用。

手動操作 (core.ops_views.op_overlay) と同じ data 形式を組むため、送出ノードの
graphic/freeform.html (および text/breaking.html) がそのまま描画できる。
CM 基本セット (context="cm") はモデル上は所有者だが、CM 中オーバーレイ表示の挙動が
提供透過動画 (段階⑤) と一体のため本 UI では未提供 (cg-layers.md 参照)。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from core import thumbnails
from core.models import Channel
from scheduling.models import GraphicCue, GraphicKind, Program, Series
from scheduling.views import _channels_ctx

_OWNERS = ("series", "program", "filler")


def _resolve_owner(channel: Channel, owner: str, owner_id: int) -> dict | None:
    """owner 種別 → {filter, create, title, subtitle, back_url}。不正なら None。"""
    if owner == "series":
        s = get_object_or_404(Series, pk=owner_id, channel=channel)
        return {
            "filter": {"series": s},
            "create": {"series": s},
            "title": f"シリーズ「{s.title}」",
            "subtitle": "繰り返し原本。週間展開時に各回 Program へコピーされる。",
            "back_url": reverse("scheduling:series_edit", args=(channel.slug, s.id)),
        }
    if owner == "program":
        p = get_object_or_404(Program, pk=owner_id, channel=channel)
        return {
            "filter": {"program": p},
            "create": {"program": p},
            "title": f"番組「{p.title}」#{p.id}",
            "subtitle": "この回のみ。Series からコピー済なら個別編集になる。",
            "back_url": reverse("scheduling:program_update", args=(channel.slug, p.id)),
        }
    if owner == "filler":
        return {
            "filter": {"channel": channel, "context": "filler"},
            "create": {"channel": channel, "context": "filler"},
            "title": "チャンネル基本セット（フィラー）",
            "subtitle": "フィラー再生中の全クリップに常時適用される基本グラフィック。",
            "back_url": reverse("scheduling:series_list", args=(channel.slug,)),
        }
    return None


def _abs_url(url: str) -> str:
    """相対 /t/... は送出ノード(CEF)から取得するため公開ベース URL で絶対化。"""
    if url.startswith("/") and settings.ICSTV_PUBLIC_BASE_URL:
        return settings.ICSTV_PUBLIC_BASE_URL.rstrip("/") + url
    return url


def _build_data(request, kind: str) -> tuple[dict | None, str | None]:
    """POST/FILES から GraphicCue.data を組む。(data, error)。op_overlay と同形式。

    生 JSON (data フィールド) があれば最優先 (複数要素の上級用)。それ以外は kind 別に組む。
    """
    raw = (request.POST.get("data") or "").strip()
    if raw:
        try:
            return json.loads(raw), None
        except ValueError:
            return None, "data が不正な JSON です"
    text = (request.POST.get("text") or "").strip()
    if kind == GraphicKind.GRAPHIC:
        url = ""
        if request.FILES.get("image"):
            try:
                url = thumbnails.store(request.FILES["image"])  # R2 → /t/<key>
            except thumbnails.ThumbnailError as e:
                return None, str(e)
        elif request.POST.get("image_url"):
            url = request.POST["image_url"].strip()
        el: dict = {}
        if url:
            el["media"] = "video" if request.POST.get("media") == "video" else "image"
            el["url"] = _abs_url(url)
            if el["media"] == "video" and request.POST.get("loop"):
                el["loop"] = True
        if text:
            el["text"] = text
        for f in ("x", "y", "w", "size"):
            v = request.POST.get(f)
            if v and v.lstrip("-").isdigit():
                el[f] = int(v)
        if request.POST.get("color"):
            el["color"] = request.POST["color"]
        if request.POST.get("align"):
            el["align"] = request.POST["align"]
        return ({"elements": [el]} if el else {}), None
    if kind == GraphicKind.VIDEO:
        d: dict = {}
        clip = (request.POST.get("clip") or "").strip()
        if clip:
            d["clip"] = clip
        if request.POST.get("loop"):
            d["loop"] = True
        return d, None
    return ({"text": text} if text else {}), None


def _sec_to_ms(v: str | None, default):
    v = (v or "").strip()
    if not v:
        return default
    try:
        return int(float(v) * 1000)
    except ValueError:
        return default


def _summary(c: GraphicCue) -> str:
    """一覧表示用の data 要約。"""
    if c.kind == GraphicKind.TEXT:
        return c.data.get("text", "") if isinstance(c.data, dict) else ""
    if c.kind == GraphicKind.VIDEO:
        loop = "🔁 " if c.data.get("loop") else ""
        return loop + (c.data.get("clip") or "(動画)")
    parts: list[str] = []
    for e in (c.data.get("elements") or []) if isinstance(c.data, dict) else []:
        if e.get("url"):
            parts.append("🎞" if e.get("media") == "video" else "🖼")
        if e.get("text"):
            parts.append(e["text"])
    return " ".join(parts) or "(空)"


def _back(slug: str, owner: str, owner_id: int, err: str | None = None):
    url = reverse("scheduling:graphic_cues", args=(slug, owner, owner_id))
    if err:
        url += "?err=" + quote(err)
    return redirect(url)


@staff_member_required
@require_http_methods(["GET"])
def graphic_cues(request, slug: str, owner: str, owner_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    spec = _resolve_owner(channel, owner, owner_id)
    if spec is None:
        return redirect("scheduling:series_list", slug=slug)
    cues = GraphicCue.objects.filter(**spec["filter"]).order_by("layer", "seq", "id")
    rows = [
        {
            "c": c,
            "summary": _summary(c),
            "show_s": c.show_at_ms / 1000,
            "hide_s": (c.hide_at_ms / 1000) if c.hide_at_ms is not None else None,
        }
        for c in cues
    ]
    return render(
        request,
        "scheduling/graphic_cues.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "owner": owner,
            "owner_id": owner_id,
            "spec": spec,
            "rows": rows,
            "kinds": GraphicKind.choices,
            "err": request.GET.get("err"),
            "active": "series",
        },
    )


@staff_member_required
@require_POST
def graphic_cue_add(request, slug: str, owner: str, owner_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    spec = _resolve_owner(channel, owner, owner_id)
    if spec is None:
        return redirect("scheduling:series_list", slug=slug)
    try:
        layer = int(request.POST.get("layer", "0"))
    except ValueError:
        layer = 0
    if not (1 <= layer <= 89):
        return _back(slug, owner, owner_id, "レイヤは 1-89 を指定してください (90=スレート予約)")
    kind = request.POST.get("kind", GraphicKind.GRAPHIC)
    data, err = _build_data(request, kind)
    if err:
        return _back(slug, owner, owner_id, err)
    try:
        seq = int(request.POST.get("seq") or 0)
    except ValueError:
        seq = 0
    GraphicCue.objects.create(
        layer=layer,
        kind=kind,
        data=data,
        template=(request.POST.get("template") or "").strip(),
        show_at_ms=_sec_to_ms(request.POST.get("show_at_s"), 0),
        hide_at_ms=_sec_to_ms(request.POST.get("hide_at_s"), None),
        seq=seq,
        **spec["create"],
    )
    return _back(slug, owner, owner_id)


@staff_member_required
@require_POST
def graphic_cue_delete(request, slug: str, owner: str, owner_id: int, cue_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    spec = _resolve_owner(channel, owner, owner_id)
    if spec is None:
        return redirect("scheduling:series_list", slug=slug)
    # 所有者一致を確認してから削除 (slug 越境防止)。
    GraphicCue.objects.filter(pk=cue_id, **spec["filter"]).delete()
    return _back(slug, owner, owner_id)
