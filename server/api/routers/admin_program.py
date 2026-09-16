# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2e-2): 番組 作成/編集フォーム。staff 限定。

タイムラインの create-by-drag / 編集 の遷移先を SPA 化。素材(録画)/live_source(生) のドロップダウン +
ソース制約 (録画=asset 必須 / 生=live_source 必須) を既存 ProgramForm の検証で担保する (form を再利用し
clean/DB CHECK をそのまま効かせる)。削除は既存エンドポイントへ。サムネ upload を伴う series フォームは別。
"""

from __future__ import annotations

from django.db import IntegrityError
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Query, Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import OkOut, ProgramFormOut, ProgramIn

router = Router(tags=["admin"], auth=staff_auth)


def _options(channel):
    from core.models import LiveSource
    from medialib.models import Asset, NormalizeStatus

    assets = [
        {"id": a.id, "name": a.title}
        for a in Asset.objects.filter(
            usable_as_program=True, normalize_status=NormalizeStatus.READY
        ).order_by("-created_at")[:300]
    ]
    live = [{"id": s.id, "name": s.name} for s in LiveSource.objects.order_by("name")]
    return assets, live


@router.get("/admin/scheduling/{slug}/program-form", response=ProgramFormOut)
def program_form(
    request: HttpRequest,
    slug: str,
    program_id: int | None = Query(None),
    start: str = Query(""),
    end: str = Query(""),
):
    from core.models import Channel
    from scheduling.models import ExposurePolicy, Program

    channel = get_object_or_404(Channel, slug=slug)
    assets, live = _options(channel)
    initial = {
        "type": "recorded",
        "public_visible": True,
        "vod_visibility": "off",
        "start_at": start,
        "end_at": end,
    }
    delete_url = ""
    if program_id:
        p = get_object_or_404(Program, pk=program_id, channel=channel)
        initial = {
            "id": p.id,
            "title": p.title,
            "type": p.type,
            "genre": p.genre or "",
            "exposure_policy": p.exposure_policy or "",
            "start_at": timezone.localtime(p.start_at).strftime("%Y-%m-%dT%H:%M:%S"),
            "end_at": timezone.localtime(p.end_at).strftime("%Y-%m-%dT%H:%M:%S"),
            "asset_id": p.asset_id,
            "live_source_id": p.live_source_id,
            "cast": p.cast or "",
            "description": p.description or "",
            "public_visible": p.public_visible,
            "vod_visibility": p.vod_visibility,
            "clock_style_override": p.clock_style_override,
            "record_live": p.record_live,
        }
        delete_url = f"/scheduling/ch/{slug}/programs/{p.id}/delete/"
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "assets": assets,
        "live_sources": live,
        "initial": initial,
        # 空 (継承) を先頭にした選択肢。空 label はシリーズ既定へのフォールバックを示す。
        "exposure_policy_choices": [{"value": "", "label": "（シリーズ既定を継承）"}]
        + [{"value": v, "label": label} for v, label in ExposurePolicy.choices],
        "delete_url": delete_url,
    }


def _save(channel, payload: ProgramIn, instance=None):
    from scheduling.views import ProgramForm

    data = {
        "title": payload.title,
        "type": payload.type,
        "genre": payload.genre,
        "exposure_policy": payload.exposure_policy,
        "start_at": payload.start_at,
        "end_at": payload.end_at,
        "asset": payload.asset_id or "",
        "live_source": payload.live_source_id or "",
        "cm_bundle": "",
        "description": payload.description,
        "cast": payload.cast,
        "public_visible": payload.public_visible,
        "vod_visibility": payload.vod_visibility or "off",
        "vod_available_until": "",
        "record_live": payload.record_live,
    }
    form = ProgramForm(data, instance=instance)
    if not form.is_valid():
        msgs = "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items())
        raise HttpError(400, msgs or "入力が不正です")
    program = form.save(commit=False)
    program.channel = channel
    program.clock_style_override = payload.clock_style_override
    try:
        program.save()
    except IntegrityError:
        raise HttpError(409, "時間が他の番組と重なります") from None
    return program


@router.post("/admin/scheduling/{slug}/programs", response=OkOut)
def create_program(request: HttpRequest, slug: str, payload: ProgramIn):
    from core.models import Channel

    channel = get_object_or_404(Channel, slug=slug)
    _save(channel, payload)
    return {"ok": True}


@router.post("/admin/scheduling/{slug}/programs/{int:program_id}", response=OkOut)
def update_program(request: HttpRequest, slug: str, program_id: int, payload: ProgramIn):
    from core.models import Channel
    from scheduling.models import Program

    channel = get_object_or_404(Channel, slug=slug)
    p = get_object_or_404(Program, pk=program_id, channel=channel)
    _save(channel, payload, instance=p)
    return {"ok": True}
