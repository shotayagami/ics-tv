# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2e-1): 自動グラフィック graphic_cues (owner 別 CG キュー)。staff 限定。

owner = series | program | filler 単位の GraphicCue を読み取り JSON 化。テキストキューの追加・削除は
既存エンドポイント (graphic_cue_add/delete) を form-POST 再利用。画像/動画アップロードを伴うリッチ編集は
複雑なため旧画面 (back_url) へ。既存 scheduling.graphic_views._resolve_owner/_summary を再利用する。
"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import GraphicCuesOut

router = Router(tags=["admin"], auth=staff_auth)


@router.get("/admin/scheduling/{slug}/graphic-cues/{owner}/{int:owner_id}", response=GraphicCuesOut)
def graphic_cues(request: HttpRequest, slug: str, owner: str, owner_id: int):
    from core.models import Channel
    from scheduling.graphic_views import _resolve_owner, _summary
    from scheduling.models import GraphicCue

    channel = get_object_or_404(Channel, slug=slug)
    spec = _resolve_owner(channel, owner, owner_id)
    if spec is None:
        raise HttpError(404, "owner が不正です")
    cues = GraphicCue.objects.filter(**spec["filter"]).order_by("layer", "seq", "id")
    rows = [
        {
            "id": c.id,
            "layer": c.layer,
            "kind": c.kind,
            "summary": _summary(c),
            "show_s": c.show_at_ms / 1000,
            "hide_s": (c.hide_at_ms / 1000) if c.hide_at_ms is not None else None,
            "seq": c.seq,
        }
        for c in cues
    ]
    return {
        "title": spec["title"],
        "subtitle": spec.get("subtitle", "") or "",
        "owner": owner,
        "owner_id": owner_id,
        "cues": rows,
        "back_url": spec["back_url"],
    }
