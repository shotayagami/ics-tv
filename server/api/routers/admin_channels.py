# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-5): チャンネル管理 (一覧 + 基本編集)。staff 限定。

name / slug / 略称(short) / 識別色(tint) / 公開(enabled) を編集する。既存 core.admin_views.channel_update
と同じ検証 (name 必須・slug 形式+一意・tint は #rrggbb) を JSON 化。YouTube OAuth / Cloudflare Live /
メディア割当 / スロットダッシュボードは複雑 (外部 OAuth 等) なので据え置き、settings_url で旧画面へ誘導。
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import (
    AdminChannel,
    ChannelSettingsOut,
    ChannelUpdateIn,
    ClockPresetIn,
    ClockPresetOut,
    ClockStyleOut,
    OkOut,
)

router = Router(tags=["admin"], auth=staff_auth)

_TINT_RE = re.compile(r"#[0-9A-Fa-f]{6}")


@router.get("/admin/channels", response=list[AdminChannel])
def channels(request: HttpRequest):
    from core.models import Channel

    return [
        {
            "id": c.id,
            "slug": c.slug,
            "name": c.name,
            "short": c.short,
            "tint": c.tint,
            "tint_color": c.tint_color,
            "enabled": c.enabled,
            "settings_url": f"/admin-ui/channels/{c.slug}/settings/",
        }
        for c in Channel.objects.order_by("slug")
    ]


@router.get("/admin/channels/{slug}/settings", response=ChannelSettingsOut)
def channel_settings(request: HttpRequest, slug: str):
    """チャンネル詳細設定 (#2e-4)。既定フィラー/スレートの割当 (基本操作) を studio で完結させ、
    YouTube OAuth / CF Live の状態を表示。OAuth 開始はサーバ描画フローへの <a> リンクのみ (JWT 化
    しない)。CF/YT 作成等の重い外部 API 操作は advanced_url の旧画面に据え置く。既存 core.admin_views.
    channel_settings と同じ集約。"""
    import os

    from core.models import Channel
    from medialib.models import Asset, AssetKind, FillerPlaylist, NormalizeStatus

    channel = get_object_or_404(Channel, slug=slug)
    cred = getattr(channel, "youtube_credential", None)
    connected = bool(cred and cred.refresh_token)
    oauth_configured = bool(
        os.environ.get("ICSTV_OAUTH_CLIENT_ID") and os.environ.get("ICSTV_OAUTH_CLIENT_SECRET")
    )
    has_config = hasattr(channel, "youtube_config")
    # スレート候補は prefetch+送出に R2 が要るため正規化済 (READY) の非番組/非CM のみ (旧 view と同条件)。
    slate_options = [
        {"id": a.id, "name": a.title}
        for a in Asset.objects.exclude(kind__in=[AssetKind.PROGRAM, AssetKind.CM])
        .filter(normalize_status=NormalizeStatus.READY)
        .order_by("title")
    ]
    filler_options = [{"id": f.id, "name": f.name} for f in FillerPlaylist.objects.order_by("name")]
    # 速報チャイム: 音源ライブラリ (局共通) + カテゴリ別の選択 (per-channel)。
    from core.models import ChannelChime, ChimeCategory, ChimeSound

    chime_library = [
        {
            "id": s.id,
            "name": s.name,
            "filename": s.original_filename,
            "preview_url": f"/admin-ui/chime/sound/{s.id}/preview/",
            "created_at": s.created_at.isoformat(),
        }
        for s in ChimeSound.objects.all()
    ]
    selected = {c.category: c.sound_id for c in ChannelChime.objects.filter(channel=channel)}
    chimes = [
        {"category": cat, "label": label, "selected_sound_id": selected.get(cat)}
        for cat, label in ChimeCategory.choices
    ]
    return {
        "slug": channel.slug,
        "name": channel.name,
        "youtube": {
            "connected": connected,
            "oauth_configured": oauth_configured,
            "has_config": has_config,
            "connect_url": f"/admin-ui/ch/{channel.slug}/youtube/connect/",
        },
        "cloudflare": {
            "live_input_id": channel.cf_live_input_id or "",
            "playback_hls_url": channel.cf_playback_hls_url or "",
            "set_playback_url": f"/admin-ui/ch/{channel.slug}/cloudflare/playback-url/",
        },
        "default_filler_id": channel.default_filler_id,
        "slate_asset_id": channel.slate_asset_id,
        "site_only_filler_id": channel.site_only_filler_id,
        "members_filler_id": channel.members_filler_id,
        "filler_options": filler_options,
        "slate_options": slate_options,
        "media_post_url": f"/admin-ui/ch/{channel.slug}/media/",
        "chime_library": chime_library,
        "chimes": chimes,
        "chime_sound_post_url": "/admin-ui/chime/sound/",
        "chime_select_url": f"/admin-ui/ch/{channel.slug}/chime/select/",
        "advanced_url": f"/admin-ui/ch/{channel.slug}/",
    }


_CLOCK_STYLE_DEFAULTS: dict = {
    "font_family": "noto-sans-jp",
    "time_size": 54,
    "font_weight": 700,
    "time_color": "#ffffff",
    "date_color": "#cfe3ff",
    "text_effect": "soft-shadow",
    "stroke_width": "none",
    "stroke_color": "#000000",
    "bg_preset": "dark-gradient",
    "bg_opacity": 0.8,
    "box_shadow": "md",
    "border_radius": "md",
    "entrance_anim": "fade",
    "show_seconds": False,
    "show_date": False,
    "position": "top-left",
}


@router.get("/admin/channels/{slug}/clock", response=ClockStyleOut)
def channel_clock(request: HttpRequest, slug: str):
    """時計エディタ設定読み取り。clock_style + enabled + windows を返す。"""
    from core.models import Channel

    channel = get_object_or_404(Channel, slug=slug)
    style = channel.clock_style or {}
    d = {**_CLOCK_STYLE_DEFAULTS, **style}
    return {
        **d,
        "clock_overlay_enabled": channel.clock_overlay_enabled,
        "clock_windows": channel.clock_windows or [],
        "save_url": f"/admin-ui/ch/{channel.slug}/clock-style/",
    }


@router.post("/admin/channels/{slug}", response=OkOut)
def update_channel(request: HttpRequest, slug: str, payload: ChannelUpdateIn):
    from core.models import Channel

    channel = get_object_or_404(Channel, slug=slug)
    name = payload.name.strip()
    new_slug = payload.slug.strip()
    if not name:
        raise HttpError(400, "表示名は必須です")
    if not new_slug:
        raise HttpError(400, "slug は必須です")
    try:
        validate_slug(new_slug)
    except ValidationError as e:
        raise HttpError(
            400, "slug の形式が不正です (半角英数・ハイフン・アンダースコアのみ)"
        ) from e
    if Channel.objects.filter(slug=new_slug).exclude(pk=channel.pk).exists():
        raise HttpError(400, f"slug が既に他のチャンネルで使用されています: {new_slug}")
    tint = payload.tint.strip()
    if tint and not _TINT_RE.fullmatch(tint):
        raise HttpError(400, "識別色は #rrggbb 形式で入力してください")
    old_slug = channel.slug
    channel.name = name[:200]
    channel.slug = new_slug
    channel.short = payload.short.strip()[:20]
    channel.tint = tint
    channel.enabled = payload.enabled
    channel.save(update_fields=["name", "slug", "short", "tint", "enabled"])
    # 選択中 ch を rename したら session も追従 (旧 view と同じ)
    if request.session.get("current_ch") == old_slug:
        request.session["current_ch"] = new_slug
    return {"ok": True}


# ---- 時計スタイルプリセット CRUD ----


def _preset_out(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "style": p.style,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


@router.get("/admin/clock-presets", response=list[ClockPresetOut])
def list_clock_presets(request: HttpRequest):
    """時計スタイルプリセット一覧。"""
    from core.models import ClockStylePreset

    return [_preset_out(p) for p in ClockStylePreset.objects.all()]


@router.post("/admin/clock-presets", response=ClockPresetOut)
def create_clock_preset(request: HttpRequest, payload: ClockPresetIn):
    """時計スタイルプリセット新規作成。スタイルはデフォルトと merge して保存する。"""
    from core.models import ClockStylePreset

    name = payload.name.strip()
    if not name:
        raise HttpError(400, "プリセット名は必須です")
    style = {**_CLOCK_STYLE_DEFAULTS, **payload.style}
    p = ClockStylePreset.objects.create(name=name, style=style)
    return _preset_out(p)


@router.put("/admin/clock-presets/{preset_id}", response=ClockPresetOut)
def update_clock_preset(request: HttpRequest, preset_id: int, payload: ClockPresetIn):
    """時計スタイルプリセット更新。"""
    from core.models import ClockStylePreset

    p = get_object_or_404(ClockStylePreset, pk=preset_id)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "プリセット名は必須です")
    p.name = name
    p.style = {**_CLOCK_STYLE_DEFAULTS, **payload.style}
    p.save()
    return _preset_out(p)


@router.delete("/admin/clock-presets/{preset_id}", response=OkOut)
def delete_clock_preset(request: HttpRequest, preset_id: int):
    """時計スタイルプリセット削除。"""
    from core.models import ClockStylePreset

    get_object_or_404(ClockStylePreset, pk=preset_id).delete()
    return {"ok": True}
