# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA: YouTube スロット dashboard + 配信プリセット CRUD + 番組専用枠。staff 限定。

* スロット: 2h×12 ローリング。状態遷移/メタ更新は既存 admin-ui form-POST 再利用。
* 配信プリセット (YoutubeBroadcastPreset): 全フィールド CRUD (#Phase2e-YTpreset)
* 番組専用枠 (ProgramBroadcast): ダッシュボード + 作成 + 遷移 + チェックリスト (#Phase2e-dedicated)
"""

from __future__ import annotations

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router

from api.auth import staff_auth
from api.schemas import (
    ChecklistStateIn,
    DedicatedDashboardOut,
    OkOut,
    PresetDetail,
    PresetFormMeta,
    PresetIn,
    PresetListItem,
    SlotListOut,
    TransitionIn,
    YtDescTemplateIn,
    YtDescTemplateOut,
    YtResyncOut,
    YtRollingConfigIn,
    YtRollingConfigOut,
)

router = Router(tags=["admin"], auth=staff_auth)


@router.get("/admin/youtube/{slug}/slots", response=SlotListOut)
def slots(request: HttpRequest, slug: str):
    from core.models import Channel
    from youtube.models import YoutubeSlot

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    rows = []
    for s in YoutubeSlot.objects.filter(channel=channel).order_by("-window_start")[:24]:
        ws = timezone.localtime(s.window_start)
        we = timezone.localtime(s.window_end)
        rows.append(
            {
                "id": s.id,
                "window": f"{ws.month}/{ws.day} {ws:%H:%M}–{we:%H:%M}",
                "status": s.status,
                "broadcast_id": s.broadcast_id or "",
                "title": s.title or "",
                "description": s.description or "",
                "manual": bool(s.manual),
                "error": s.error or "",
            }
        )
    channels = [
        {"slug": c.slug, "name": c.name}
        for c in Channel.objects.filter(enabled=True).order_by("slug")
    ]
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "channels": channels,
        "slots": rows,
        "settings_url": f"/admin-ui/channels/{channel.slug}/settings/",
    }


# --- rolling 枠生成テンプレ (YoutubeConfig・per-channel) ---

# YtPrivacy の選択肢 (public/unlisted/private)。フロントの select 用。
_YT_PRIVACY_CHOICES = ["public", "unlisted", "private"]

# YoutubeConfig の既定値 (未設定チャンネルの提示 + upsert 時の defaults)。モデル既定と一致させる。
_YT_CFG_DEFAULTS = {
    "title_template": "{channel} {date} {start}-{end}",
    "description_template": "ICS-TV {date} {start}-{end} (JST) の配信枠です。\n\n{programs}",
    "privacy": "public",
    "enable_monitor": False,
    "rolling_hours": 24,
    "slot_minutes": 240,
    "nudge_lead_minutes": 10,
    "nudge_template": "まもなくこの配信は終了します。続きは次の配信でご覧ください ▶ {url}",
    "nudge_ended_template": "この配信は終了しています。続きは次の配信でご覧ください ▶ {url}",
}


@router.get("/admin/youtube/{slug}/config", response=YtRollingConfigOut)
def get_rolling_config(request: HttpRequest, slug: str):
    """rolling 枠を駆動する YoutubeConfig を返す。未設定ならモデル既定値を exists=False で提示。"""
    from core.models import Channel
    from youtube.models import YoutubeConfig

    channel = get_object_or_404(Channel, slug=slug)
    cfg = YoutubeConfig.objects.filter(channel=channel).first()
    src = cfg or None
    return {
        "exists": cfg is not None,
        "title_template": (src.title_template if src else _YT_CFG_DEFAULTS["title_template"]),
        "description_template": (
            src.description_template if src else _YT_CFG_DEFAULTS["description_template"]
        ),
        "privacy": (src.privacy if src else _YT_CFG_DEFAULTS["privacy"]),
        "enable_monitor": (src.enable_monitor if src else _YT_CFG_DEFAULTS["enable_monitor"]),
        "rolling_hours": (src.rolling_hours if src else _YT_CFG_DEFAULTS["rolling_hours"]),
        "slot_minutes": (src.slot_minutes if src else _YT_CFG_DEFAULTS["slot_minutes"]),
        "nudge_lead_minutes": (
            src.nudge_lead_minutes if src else _YT_CFG_DEFAULTS["nudge_lead_minutes"]
        ),
        "nudge_template": (src.nudge_template if src else _YT_CFG_DEFAULTS["nudge_template"]),
        "nudge_ended_template": (
            src.nudge_ended_template if src else _YT_CFG_DEFAULTS["nudge_ended_template"]
        ),
        "privacy_choices": _YT_PRIVACY_CHOICES,
    }


@router.post("/admin/youtube/{slug}/config", response=YtRollingConfigOut)
def save_rolling_config(request: HttpRequest, slug: str, payload: YtRollingConfigIn):
    """YoutubeConfig を upsert。既存枠への反映は別途 resync が必要 (説明明記)。"""
    from ninja.errors import HttpError

    from core.models import Channel
    from youtube.models import YoutubeConfig, YtPrivacy

    channel = get_object_or_404(Channel, slug=slug)
    if payload.privacy not in _YT_PRIVACY_CHOICES:
        raise HttpError(400, f"privacy は {_YT_PRIVACY_CHOICES} のいずれか")
    if payload.rolling_hours < 1 or payload.slot_minutes < 1:
        raise HttpError(400, "rolling_hours / slot_minutes は 1 以上")
    cfg, _ = YoutubeConfig.objects.get_or_create(channel=channel)
    cfg.title_template = payload.title_template
    cfg.description_template = payload.description_template
    cfg.privacy = YtPrivacy(payload.privacy)
    cfg.enable_monitor = payload.enable_monitor
    cfg.rolling_hours = payload.rolling_hours
    cfg.slot_minutes = payload.slot_minutes
    cfg.nudge_lead_minutes = payload.nudge_lead_minutes
    cfg.nudge_template = payload.nudge_template
    cfg.nudge_ended_template = payload.nudge_ended_template
    cfg.save()
    return get_rolling_config(request, slug)


@router.post("/admin/youtube/{slug}/config/resync", response=YtResyncOut)
def resync_rolling_config(request: HttpRequest, slug: str, include_manual: bool = False):
    """現テンプレを未終了の既存枠へ再生成・反映 (resync_youtube_slot_meta 相当)。"""
    from ninja.errors import HttpError

    from core.models import Channel
    from youtube.models import YoutubeConfig
    from youtube.tasks import resync_slot_meta

    channel = get_object_or_404(Channel, slug=slug)
    if not YoutubeConfig.objects.filter(channel=channel).exists():
        raise HttpError(412, "youtube_config 未設定 (先に保存してください)")
    stats = resync_slot_meta(channel.id, include_manual=include_manual)
    return {"ok": True, "stats": stats}


# --- YouTube 説明テンプレート管理 (チャンネル非依存マスタ) ---


@router.get("/admin/youtube/description-templates", response=list[YtDescTemplateOut])
def list_description_templates(request: HttpRequest):
    """登録済み説明テンプレート一覧 (name 昇順)。"""
    from youtube.models import YoutubeDescriptionTemplate

    return [
        {
            "id": t.id,
            "name": t.name,
            "title_template": t.title_template,
            "description_template": t.description_template,
        }
        for t in YoutubeDescriptionTemplate.objects.all()
    ]


@router.post("/admin/youtube/description-templates", response=YtDescTemplateOut)
def create_description_template(request: HttpRequest, payload: YtDescTemplateIn):
    """説明テンプレートを新規作成して返す。"""
    from youtube.models import YoutubeDescriptionTemplate

    t = YoutubeDescriptionTemplate.objects.create(
        name=payload.name,
        title_template=payload.title_template,
        description_template=payload.description_template,
    )
    return {
        "id": t.id,
        "name": t.name,
        "title_template": t.title_template,
        "description_template": t.description_template,
    }


@router.post("/admin/youtube/description-templates/{template_id}", response=YtDescTemplateOut)
def update_description_template(request: HttpRequest, template_id: int, payload: YtDescTemplateIn):
    """既存説明テンプレートを上書き更新して返す。"""
    from youtube.models import YoutubeDescriptionTemplate

    t = get_object_or_404(YoutubeDescriptionTemplate, id=template_id)
    t.name = payload.name
    t.title_template = payload.title_template
    t.description_template = payload.description_template
    t.save()
    return {
        "id": t.id,
        "name": t.name,
        "title_template": t.title_template,
        "description_template": t.description_template,
    }


@router.delete("/admin/youtube/description-templates/{template_id}", response=OkOut)
def delete_description_template(request: HttpRequest, template_id: int):
    """説明テンプレートを削除する。"""
    from youtube.models import YoutubeDescriptionTemplate

    t = get_object_or_404(YoutubeDescriptionTemplate, id=template_id)
    t.delete()
    return {"ok": True}


# --- 配信プリセット CRUD (#Phase2e-YTpreset) ---

_YT_CATEGORY_CHOICES = [
    (None, "（未設定）"),
    (24, "エンターテイメント"),
    (20, "ゲーム"),
    (23, "コメディ"),
    (17, "スポーツ"),
    (25, "ニュースと政治"),
    (26, "ハウツーとスタイル"),
    (22, "ブログ"),
    (15, "ペットと動物"),
    (1, "映画とアニメ"),
    (10, "音楽"),
    (28, "科学と技術"),
    (27, "教育"),
    (2, "自動車と乗り物"),
    (29, "非営利団体と社会活動"),
    (19, "旅行とイベント"),
]


def _preset_to_detail(p) -> dict:
    from youtube.models import default_manual_checklist

    checklist = p.manual_checklist or default_manual_checklist()
    return {
        "id": p.id,
        "name": p.name,
        "channel_id": p.channel_id,
        "title_template": p.title_template or "",
        "description_template": p.description_template or "",
        "category_id": p.category_id,
        "tags": p.tags or [],
        "privacy": p.privacy,
        "made_for_kids": bool(p.made_for_kids),
        "default_language": p.default_language or "",
        "default_audio_language": p.default_audio_language or "",
        "latency": p.latency,
        "enable_dvr": bool(p.enable_dvr),
        "enable_embed": bool(p.enable_embed),
        "enable_auto_start": bool(p.enable_auto_start),
        "enable_auto_stop": bool(p.enable_auto_stop),
        "record_from_start": bool(p.record_from_start),
        "license": p.license,
        "public_stats_viewable": bool(p.public_stats_viewable),
        "thumbnail_id": p.thumbnail_id,
        "playlist_id": p.playlist_id or "",
        "manual_checklist": [
            {"key": c["key"], "label": c["label"], "default": bool(c.get("default"))}
            for c in checklist
        ],
    }


def _apply_preset_payload(p, payload: PresetIn) -> None:
    from core.models import Channel
    from medialib.models import Asset

    p.name = payload.name
    p.channel = (
        Channel.objects.filter(id=payload.channel_id).first() if payload.channel_id else None
    )
    p.title_template = payload.title_template
    p.description_template = payload.description_template
    p.category_id = payload.category_id
    p.tags = payload.tags
    p.privacy = payload.privacy
    p.made_for_kids = payload.made_for_kids
    p.default_language = payload.default_language
    p.default_audio_language = payload.default_audio_language
    p.latency = payload.latency
    p.enable_dvr = payload.enable_dvr
    p.enable_embed = payload.enable_embed
    p.enable_auto_start = payload.enable_auto_start
    p.enable_auto_stop = payload.enable_auto_stop
    p.record_from_start = payload.record_from_start
    p.license = payload.license
    p.public_stats_viewable = payload.public_stats_viewable
    p.thumbnail = (
        Asset.objects.filter(id=payload.thumbnail_id).first() if payload.thumbnail_id else None
    )
    p.playlist_id = payload.playlist_id
    if payload.manual_checklist:
        p.manual_checklist = [
            {"key": c.key, "label": c.label, "default": c.default} for c in payload.manual_checklist
        ]


@router.get("/admin/youtube/presets", response=list[PresetListItem])
def list_presets(request: HttpRequest):
    """配信プリセット一覧。"""
    from youtube.models import YoutubeBroadcastPreset

    return [
        {
            "id": p.id,
            "name": p.name,
            "channel_name": p.channel.name if p.channel else None,
            "privacy": p.privacy,
            "latency": p.latency,
            "category_id": p.category_id,
        }
        for p in YoutubeBroadcastPreset.objects.select_related("channel").order_by(
            "channel", "name"
        )
    ]


@router.get("/admin/youtube/presets/form", response=PresetFormMeta)
def preset_form_meta(request: HttpRequest):
    """プリセット作成/編集フォームのメタデータ (チャンネル選択肢・カテゴリ選択肢)。"""
    from core.models import Channel

    return {
        "channels": [
            {"id": c.id, "slug": c.slug, "name": c.name}
            for c in Channel.objects.filter(enabled=True).order_by("slug")
        ],
        "category_choices": [{"value": v, "label": label} for v, label in _YT_CATEGORY_CHOICES],
    }


@router.get("/admin/youtube/presets/{preset_id}", response=PresetDetail)
def get_preset(request: HttpRequest, preset_id: int):
    """配信プリセット詳細。"""
    from youtube.models import YoutubeBroadcastPreset

    p = get_object_or_404(YoutubeBroadcastPreset, id=preset_id)
    return _preset_to_detail(p)


@router.post("/admin/youtube/presets", response=PresetDetail)
def create_preset(request: HttpRequest, payload: PresetIn):
    """配信プリセットを新規作成する。"""
    from youtube.models import YoutubeBroadcastPreset, default_manual_checklist

    p = YoutubeBroadcastPreset(manual_checklist=default_manual_checklist())
    _apply_preset_payload(p, payload)
    p.save()
    return _preset_to_detail(p)


@router.post("/admin/youtube/presets/{preset_id}", response=PresetDetail)
def update_preset(request: HttpRequest, preset_id: int, payload: PresetIn):
    """配信プリセットを更新する。"""
    from youtube.models import YoutubeBroadcastPreset

    p = get_object_or_404(YoutubeBroadcastPreset, id=preset_id)
    _apply_preset_payload(p, payload)
    p.save()
    return _preset_to_detail(p)


@router.delete("/admin/youtube/presets/{preset_id}", response=OkOut)
def delete_preset(request: HttpRequest, preset_id: int):
    """配信プリセットを削除する。"""
    from youtube.models import YoutubeBroadcastPreset

    p = get_object_or_404(YoutubeBroadcastPreset, id=preset_id)
    p.delete()
    return {"ok": True}


# --- 番組専用枠ダッシュボード (#Phase2e-dedicated) ---


def _broadcast_to_row(pb) -> dict:
    program = pb.program
    from django.utils import timezone as tz

    start = tz.localtime(program.start_at)
    end = tz.localtime(program.end_at)
    checklist_defs = pb.preset.manual_checklist if pb.preset else []
    state = pb.checklist_state or {}
    watch_url = f"https://www.youtube.com/watch?v={pb.broadcast_id}" if pb.broadcast_id else ""
    return {
        "id": pb.id,
        "program_id": program.id,
        "program_title": program.title,
        "start_at": start.strftime("%Y-%m-%d %H:%M"),
        "end_at": end.strftime("%Y-%m-%d %H:%M"),
        "status": pb.status,
        "manual": bool(pb.manual),
        "broadcast_id": pb.broadcast_id or "",
        "watch_url": watch_url,
        "preset_name": pb.preset.name if pb.preset else "（なし）",
        "checklist": [
            {
                "key": c["key"],
                "label": c["label"],
                "checked": bool(state.get(c["key"], c.get("default", False))),
            }
            for c in checklist_defs
        ],
    }


@router.get("/admin/youtube/{slug}/dedicated", response=DedicatedDashboardOut)
def dedicated_dashboard(request: HttpRequest, slug: str):
    """番組専用枠ダッシュボード。候補番組と既存 ProgramBroadcast 一覧。"""
    from django.utils import timezone as tz

    from core.models import Channel
    from scheduling.models import Program
    from youtube.models import ProgramBroadcast

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = tz.now()

    # 候補: series.youtube_dedicated=True + preset設定済 + 枠未作成 + 未来の番組
    existing_pb_program_ids = set(
        ProgramBroadcast.objects.filter(program__channel=channel).values_list(
            "program_id", flat=True
        )
    )
    candidate_programs = (
        Program.objects.filter(
            channel=channel,
            start_at__gte=now,
            series__youtube_dedicated=True,
            series__youtube_preset__isnull=False,
        )
        .select_related("series__youtube_preset")
        .order_by("start_at")[:20]
    )
    candidates = [
        {
            "program_id": p.id,
            "title": p.title,
            "start_at": tz.localtime(p.start_at).strftime("%Y-%m-%d %H:%M"),
            "preset_id": p.series.youtube_preset_id,
            "preset_name": p.series.youtube_preset.name,
        }
        for p in candidate_programs
        if p.id not in existing_pb_program_ids
        and p.series is not None
        and p.series.youtube_preset is not None
    ]

    broadcasts = (
        ProgramBroadcast.objects.filter(program__channel=channel)
        .select_related("program", "preset")
        .order_by("-program__start_at")[:50]
    )

    channels = [
        {"slug": c.slug, "name": c.name}
        for c in Channel.objects.filter(enabled=True).order_by("slug")
    ]

    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "channels": channels,
        "candidates": candidates,
        "broadcasts": [_broadcast_to_row(pb) for pb in broadcasts],
        "settings_url": f"/admin-ui/ch/{channel.slug}/",
    }


@router.post("/admin/youtube/{slug}/dedicated/{program_id}/create", response=OkOut)
def dedicated_create(request: HttpRequest, slug: str, program_id: int):
    """番組専用枠を今すぐ作成する (created 状態で登録、エージェントが apply)。"""
    from core.models import Channel
    from scheduling.models import Program
    from youtube.models import ProgramBroadcast

    channel = get_object_or_404(Channel, slug=slug)
    program = get_object_or_404(Program, id=program_id, channel=channel)
    series = program.series
    if not series or not series.youtube_dedicated or not series.youtube_preset_id:
        from ninja.errors import HttpError

        raise HttpError(400, "シリーズに youtube_dedicated=True かつ preset が必要です")
    if ProgramBroadcast.objects.filter(program=program).exists():
        from ninja.errors import HttpError

        raise HttpError(400, "すでに専用枠が存在します")
    preset = series.youtube_preset  # series/preset non-null validated above
    ProgramBroadcast.objects.create(
        program=program,
        preset=preset,
        checklist_state=preset.initial_checklist_state() if preset else {},
        manual=True,
    )
    return {"ok": True}


@router.post("/admin/youtube/program-broadcast/{pb_id}/transition", response=OkOut)
def dedicated_transition(request: HttpRequest, pb_id: int, payload: TransitionIn):
    """状態遷移: go_live (ready→live) または complete (live→complete)。"""
    from ninja.errors import HttpError

    from youtube.models import ProgramBroadcast, YtSlotStatus

    pb = get_object_or_404(ProgramBroadcast, id=pb_id)
    if payload.action == "go_live":
        if pb.status not in (YtSlotStatus.READY, YtSlotStatus.TESTING):
            raise HttpError(400, f"go_live は ready/testing 状態のみ可能 (現在: {pb.status})")
        pb.status = YtSlotStatus.LIVE
    elif payload.action == "complete":
        if pb.status != YtSlotStatus.LIVE:
            raise HttpError(400, f"complete は live 状態のみ可能 (現在: {pb.status})")
        pb.status = YtSlotStatus.COMPLETE
    else:
        raise HttpError(400, f"不明な action: {payload.action}")
    pb.save(update_fields=["status"])
    return {"ok": True}


@router.post("/admin/youtube/program-broadcast/{pb_id}/checklist", response=OkOut)
def dedicated_checklist(request: HttpRequest, pb_id: int, payload: ChecklistStateIn):
    """チェックリスト状態を保存する。"""
    from youtube.models import ProgramBroadcast

    pb = get_object_or_404(ProgramBroadcast, id=pb_id)
    pb.checklist_state = {k: bool(v) for k, v in payload.state.items()}
    pb.save(update_fields=["checklist_state"])
    return {"ok": True}


@router.delete("/admin/youtube/program-broadcast/{pb_id}", response=OkOut)
def dedicated_delete(request: HttpRequest, pb_id: int):
    """番組専用枠を削除する。"""
    from youtube.models import ProgramBroadcast

    pb = get_object_or_404(ProgramBroadcast, id=pb_id)
    pb.delete()
    return {"ok": True}
