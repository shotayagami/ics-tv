# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-7): 週間編成 series の一覧/スロット概観。staff 限定。

series + その繰り返しスロット (曜日/時刻/尺/ソース) を読み取りで JSON 化。展開 (4週先まで Program へ) /
series 削除 / slot 削除は既存エンドポイント (redirect 系) を SPA から form-POST で再利用。series の
作成/編集フォーム (サムネ upload + 繰り返し設定 + ソース選択) は複雑なため据え置き、edit_url/new_url で旧画面へ。

フォーム定義 (AudienceForm) と投稿 (AudienceSubmission) の studio admin API もここで提供。
"""

from __future__ import annotations

import csv
import io

from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, Query, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from api.auth import staff_auth
from api.schemas import (
    AudienceFormAdminOut,
    AudienceFormIn,
    AudienceSubmissionAdminOut,
    EpisodeIn,
    EpisodeOut,
    IdName,
    OkOut,
    SeriesCreated,
    SeriesCreateIn,
    SeriesFormOut,
    SeriesFullIn,
    SeriesOut,
    SeriesPostAdminOut,
    SeriesPostIn,
    SlotFormOut,
    SlotIn,
    ThumbnailOut,
    YtPresetIn,
)
from core.sheets import escape_formula

router = Router(tags=["admin"], auth=staff_auth)

_DOW_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _slot_source(slot) -> str:
    if slot.program_type == "live":
        return slot.live_source.name if slot.live_source else ""
    return slot.default_asset.title if slot.default_asset else ""


@router.post("/admin/scheduling/{slug}/series", response=SeriesCreated)
def series_create(request: HttpRequest, slug: str, payload: SeriesCreateIn):
    """週間グリッドのモーダルからシリーズをインライン作成 (title のみ必須・他はデフォルト)。

    slug は title から自動採番する。
    """
    from core.models import Channel
    from scheduling.models import Series
    from scheduling.services import slugify_unique

    channel = get_object_or_404(Channel, slug=slug)
    title = payload.title.strip()
    if not title:
        raise HttpError(422, "タイトルは必須です")
    series_slug = slugify_unique(title, channel.id)
    s = Series.objects.create(channel=channel, title=title, slug=series_slug)
    return {"id": s.id, "title": s.title}


@router.get("/admin/scheduling/{slug}/series", response=SeriesOut)
def series(request: HttpRequest, slug: str):
    from core.models import Channel
    from scheduling.models import Series

    channel = get_object_or_404(Channel, slug=slug)
    rows = []
    for s in (
        Series.objects.filter(channel=channel)
        .prefetch_related("slots", "slots__live_source", "slots__default_asset")
        .order_by("-is_active", "title")
    ):
        slots = sorted(s.slots.all(), key=lambda x: (x.dow, x.start_time))
        rows.append(
            {
                "id": s.id,
                "title": s.title,
                "genre": s.genre or "",
                "is_active": s.is_active,
                "n_slots": len(slots),
                "slug": s.slug or "",
                "edit_url": f"/scheduling/ch/{slug}/series/{s.id}/",
                "slots": [
                    {
                        "id": sl.id,
                        "recurrence": sl.recurrence_label(),
                        "start_time": sl.start_time.strftime("%H:%M"),
                        "duration": f"{sl.duration_ms // 60000}分",
                        "program_type": sl.program_type,
                        "source": _slot_source(sl),
                    }
                    for sl in slots
                ],
            }
        )
    channels = [{"slug": c.slug, "name": c.name} for c in Channel.objects.order_by("slug")]
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "series": rows,
        "new_url": f"/scheduling/ch/{slug}/series/",
        "channels": channels,
    }


# --------------------------------------------------------------------------- #
#  series 作成/編集フォーム + スロット CRUD + 回(Episode) (旧画面の SPA 化)      #
#                                                                              #
#  既存 Django フォーム (SeriesForm / SeriesSlotForm) をサーバ側で再利用して    #
#  検証/保存する (admin_program._save と同じトリック)。検証の二重実装を避ける。  #
# --------------------------------------------------------------------------- #


def _slot_options():
    """スロットの default_asset / live_source 候補 (program-form と同条件)。"""
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


@router.get("/admin/scheduling/{slug}/series-form", response=SeriesFormOut)
def series_form(request: HttpRequest, slug: str, series_id: int | None = Query(None)):
    """シリーズ作成/編集フォームの初期値 + 選択肢。series_id 無し=新規。"""
    from core.models import Channel
    from scheduling.models import ContentRating, ExposurePolicy, Genre, Series
    from youtube.models import YoutubeBroadcastPreset

    channel = get_object_or_404(Channel, slug=slug)
    presets = [
        {"id": p.id, "name": p.name}
        for p in YoutubeBroadcastPreset.objects.filter(
            Q(channel=channel) | Q(channel__isnull=True)
        ).order_by("name")
    ]
    initial: dict = {"is_active": True}
    thumbnail_post_url = ""
    delete_url = ""
    if series_id:
        s = get_object_or_404(Series, pk=series_id, channel=channel)
        initial = {
            "id": s.id,
            "title": s.title,
            "slug": s.slug or "",
            "genre": s.genre or "",
            "rating": s.rating or "",
            "exposure_policy_default": s.exposure_policy_default,
            "description": s.description or "",
            "cast": s.cast or "",
            "thumbnail_url": s.thumbnail_url or "",
            "x_handle": s.x_handle or "",
            "x_hashtag": s.x_hashtag or "",
            "is_active": s.is_active,
            "clock_hidden": s.clock_hidden,
            "lbar_hidden": s.lbar_hidden,
            "clock_style_override": s.clock_style_override,
            "youtube_dedicated": s.youtube_dedicated,
            "youtube_preset_id": s.youtube_preset_id,
        }
        thumbnail_post_url = f"/api/v1/admin/scheduling/{slug}/series/{s.id}/thumbnail"
        delete_url = f"/scheduling/ch/{slug}/series/{s.id}/delete/"
    return {
        "channel": {"slug": channel.slug, "name": channel.name},
        "initial": initial,
        "genre_choices": [{"value": v, "label": lbl} for v, lbl in Genre.choices],
        "rating_choices": [{"value": v, "label": lbl} for v, lbl in ContentRating.choices],
        "exposure_policy_choices": [
            {"value": v, "label": lbl} for v, lbl in ExposurePolicy.choices
        ],
        "youtube_presets": presets,
        "thumbnail_post_url": thumbnail_post_url,
        "delete_url": delete_url,
    }


def _save_series(channel, payload: SeriesFullIn, instance=None):
    """SeriesForm を再利用して検証/保存 (clean_slug 等をそのまま効かせる)。"""
    from scheduling.forms import SeriesForm

    data = {
        "title": payload.title,
        "slug": payload.slug,
        "genre": payload.genre,
        "rating": payload.rating,
        "exposure_policy_default": payload.exposure_policy_default,
        "description": payload.description,
        "cast": payload.cast,
        "thumbnail_url": payload.thumbnail_url,
        "x_handle": payload.x_handle,
        "x_hashtag": payload.x_hashtag,
        "is_active": payload.is_active,
        "clock_hidden": payload.clock_hidden,
        "lbar_hidden": payload.lbar_hidden,
        "youtube_dedicated": payload.youtube_dedicated,
        "youtube_preset": payload.youtube_preset_id or "",
    }
    form = SeriesForm(data, instance=instance)
    if not form.is_valid():
        msgs = "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items())
        raise HttpError(422, msgs or "入力が不正です")
    s = form.save(commit=False)
    s.channel = channel
    s.clock_style_override = payload.clock_style_override
    try:
        s.save()
    except IntegrityError:
        raise HttpError(409, "スラッグが既に使われています") from None
    return s


@router.post("/admin/scheduling/{slug}/series-full", response=SeriesCreated)
def series_full_create(request: HttpRequest, slug: str, payload: SeriesFullIn):
    """シリーズをフォーム全項目で新規作成 (週グリッドの title-only 作成とは別経路)。"""
    from core.models import Channel

    channel = get_object_or_404(Channel, slug=slug)
    s = _save_series(channel, payload)
    return {"id": s.id, "title": s.title}


@router.post("/admin/scheduling/{slug}/series/{int:series_id}", response=SeriesCreated)
def series_update(request: HttpRequest, slug: str, series_id: int, payload: SeriesFullIn):
    """シリーズのメタデータを更新。"""
    from core.models import Channel
    from scheduling.models import Series

    channel = get_object_or_404(Channel, slug=slug)
    s = get_object_or_404(Series, pk=series_id, channel=channel)
    s = _save_series(channel, payload, instance=s)
    return {"id": s.id, "title": s.title}


@router.post("/admin/scheduling/{slug}/series/{int:series_id}/thumbnail", response=ThumbnailOut)
def series_thumbnail(
    request: HttpRequest, slug: str, series_id: int, file: UploadedFile = File(...)
):
    """サムネ画像を R2 に保存し thumbnail_url を更新 (multipart)。"""
    from core import thumbnails
    from core.models import Channel
    from scheduling.models import Series

    channel = get_object_or_404(Channel, slug=slug)
    s = get_object_or_404(Series, pk=series_id, channel=channel)
    try:
        url = thumbnails.store(file)
    except thumbnails.ThumbnailError as exc:
        raise HttpError(400, str(exc)) from exc
    s.thumbnail_url = url
    s.save(update_fields=["thumbnail_url"])
    return {"thumbnail_url": url}


# ----------------------------- スロット ------------------------------------ #

_DOW_CHOICES = [(0, "月"), (1, "火"), (2, "水"), (3, "木"), (4, "金"), (5, "土"), (6, "日")]


@router.get("/admin/scheduling/{slug}/series/{int:series_id}/slot-form", response=SlotFormOut)
def slot_form(request: HttpRequest, slug: str, series_id: int, slot_id: int | None = Query(None)):
    """スロット作成/編集フォームの初期値 + 選択肢。slot_id 無し=新規。"""
    from core.models import Channel
    from scheduling.models import RecurrenceKind, Series, SeriesSlot

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    assets, live = _slot_options()
    initial: dict = {"dow": 0, "program_type": "recorded", "recurrence_kind": "weekly"}
    if slot_id:
        sl = get_object_or_404(SeriesSlot, pk=slot_id, series=series)
        p = sl.recurrence_param or {}
        initial = {
            "id": sl.id,
            "dow": sl.dow,
            "start_time": sl.start_time.strftime("%H:%M"),
            "duration_min": sl.duration_ms // 60000,
            "program_type": sl.program_type,
            "default_asset_id": sl.default_asset_id,
            "live_source_id": sl.live_source_id,
            "effective_from": sl.effective_from.strftime("%Y-%m-%d"),
            "effective_to": sl.effective_to.strftime("%Y-%m-%d") if sl.effective_to else "",
            "recurrence_kind": sl.recurrence_kind,
            "weeks_csv": ",".join(str(x) for x in p.get("weeks", [])),
            "days_csv": ",".join(str(x) for x in p.get("days", [])),
            "ending_csv": ",".join(str(x) for x in p.get("ending", [])),
        }
    return {
        "initial": initial,
        "assets": assets,
        "live_sources": live,
        "dow_choices": [{"value": str(v), "label": lbl} for v, lbl in _DOW_CHOICES],
        "recurrence_choices": [{"value": v, "label": lbl} for v, lbl in RecurrenceKind.choices],
    }


def _slot_data(payload: SlotIn) -> dict:
    return {
        "dow": payload.dow,
        "start_time": payload.start_time,
        "duration_min": payload.duration_min,
        "program_type": payload.program_type,
        "default_asset": payload.default_asset_id or "",
        "live_source": payload.live_source_id or "",
        "effective_from": payload.effective_from,
        "effective_to": payload.effective_to or "",
        "recurrence_kind": payload.recurrence_kind or "",
        "weeks_csv": payload.weeks_csv or "",
        "days_csv": payload.days_csv or "",
        "ending_csv": payload.ending_csv or "",
    }


def _slot_form(payload: SlotIn, instance=None):
    from scheduling.forms import SeriesSlotForm

    form = SeriesSlotForm(_slot_data(payload), instance=instance)
    if not form.is_valid():
        msgs = "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items())
        raise HttpError(422, msgs or "入力が不正です")
    return form


@router.post("/admin/scheduling/{slug}/series/{int:series_id}/slots", response=OkOut)
def slot_create(request: HttpRequest, slug: str, series_id: int, payload: SlotIn):
    """週間スロットを作成。"""
    from core.models import Channel
    from scheduling.models import Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    form = _slot_form(payload)
    obj = form.save(commit=False)
    obj.series = series
    obj.save()
    return {"ok": True}


@router.post("/admin/scheduling/{slug}/slots/{int:slot_id}", response=OkOut)
def slot_update(request: HttpRequest, slug: str, slot_id: int, payload: SlotIn):
    """週間スロットを更新。"""
    from core.models import Channel
    from scheduling.models import SeriesSlot

    channel = get_object_or_404(Channel, slug=slug)
    sl = get_object_or_404(SeriesSlot, pk=slot_id, series__channel=channel)
    form = _slot_form(payload, instance=sl)
    obj = form.save(commit=False)
    obj.save()
    return {"ok": True}


@router.delete("/admin/scheduling/{slug}/slots/{int:slot_id}", response=OkOut)
def slot_delete(request: HttpRequest, slug: str, slot_id: int):
    """週間スロットを削除 (展開済み Program は残る)。"""
    from core.models import Channel
    from scheduling.models import SeriesSlot

    channel = get_object_or_404(Channel, slug=slug)
    sl = get_object_or_404(SeriesSlot, pk=slot_id, series__channel=channel)
    sl.delete()
    return {"ok": True}


@router.post("/admin/scheduling/{slug}/series/{int:series_id}/slots/preview", response=list[str])
def slot_preview(request: HttpRequest, slug: str, series_id: int, payload: SlotIn):
    """入力値 (未保存) でこのスロットが今後生成する放送予定日を返す (展開前プレビュー)。"""
    from core.models import Channel
    from scheduling.models import Series

    channel = get_object_or_404(Channel, slug=slug)
    get_object_or_404(Series, pk=series_id, channel=channel)
    form = _slot_form(payload)
    obj = form.save(commit=False)  # DB 未保存
    return [d.isoformat() for d in obj.upcoming_dates(n=10, horizon_days=180)]


# ----------------------------- 回 (Episode) -------------------------------- #
#  Episode は scheduling 所有 (delivery cutover 後も ICS-TV 残留)。asset は     #
#  delivery seam (internal.delivery_asset) が確定するため studio は書き込まない。#


def _episode_out(ep) -> dict:
    from scheduling.models import EpisodeStatus

    return {
        "id": ep.id,
        "episode_no": ep.episode_no,
        "air_date": ep.air_date.strftime("%Y-%m-%d") if ep.air_date else "",
        "title": ep.title or "",
        "status": ep.status,
        "status_label": EpisodeStatus(ep.status).label,
        "asset_id": ep.asset_id,
        "asset_title": ep.asset.title if ep.asset_id else "",
    }


@router.get("/admin/scheduling/{slug}/series/{int:series_id}/episodes", response=list[EpisodeOut])
def episodes_list(request: HttpRequest, slug: str, series_id: int):
    """シリーズの回 (予定/確定/放送済) 一覧。"""
    from core.models import Channel
    from scheduling.models import Episode, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    eps = (
        Episode.objects.filter(series=series)
        .select_related("asset")
        .order_by("-air_date", "-episode_no", "-id")
    )
    return [_episode_out(ep) for ep in eps]


def _parse_date(s: str):
    from django.utils.dateparse import parse_date

    if not s:
        return None
    d = parse_date(s)
    if s and d is None:
        raise HttpError(422, "放送日は YYYY-MM-DD 形式で入力してください")
    return d


def _apply_episode(ep, payload: EpisodeIn) -> None:
    from scheduling.models import EpisodeStatus

    if payload.status not in EpisodeStatus.values:
        raise HttpError(422, "不正な状態です")
    ep.episode_no = payload.episode_no
    ep.air_date = _parse_date(payload.air_date)
    ep.title = (payload.title or "").strip()
    ep.status = payload.status
    try:
        ep.save()
    except IntegrityError:
        raise HttpError(409, "その回数は既に使われています") from None


@router.post("/admin/scheduling/{slug}/series/{int:series_id}/episodes", response=EpisodeOut)
def episode_create(request: HttpRequest, slug: str, series_id: int, payload: EpisodeIn):
    """予定回を確保 (asset は納品で確定するため未設定)。"""
    from core.models import Channel
    from scheduling.models import Episode, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    ep = Episode(series=series)
    _apply_episode(ep, payload)
    return _episode_out(ep)


@router.post("/admin/scheduling/{slug}/episodes/{int:episode_id}", response=EpisodeOut)
def episode_update(request: HttpRequest, slug: str, episode_id: int, payload: EpisodeIn):
    """回のメタ (回数/放送日/タイトル/状態) を更新。asset は変更しない。"""
    from core.models import Channel
    from scheduling.models import Episode

    channel = get_object_or_404(Channel, slug=slug)
    ep = get_object_or_404(Episode, pk=episode_id, series__channel=channel)
    _apply_episode(ep, payload)
    return _episode_out(ep)


@router.delete("/admin/scheduling/{slug}/episodes/{int:episode_id}", response=OkOut)
def episode_delete(request: HttpRequest, slug: str, episode_id: int):
    """予定回 (素材未確定) のみ削除可。確定/放送済はガード。"""
    from core.models import Channel
    from scheduling.models import Episode

    channel = get_object_or_404(Channel, slug=slug)
    ep = get_object_or_404(Episode, pk=episode_id, series__channel=channel)
    if ep.asset_id:
        raise HttpError(409, "納品で素材が確定した回は削除できません")
    ep.delete()
    return {"ok": True}


# ----------------------------- YouTube preset ------------------------------- #


@router.post("/admin/scheduling/{slug}/youtube-preset", response=IdName)
def youtube_preset_create(request: HttpRequest, slug: str, payload: YtPresetIn):
    """YouTube 配信プリセットをシリーズ編集フォームからインライン作成。"""
    from core.models import Channel
    from youtube.models import YoutubeBroadcastPreset

    channel = get_object_or_404(Channel, slug=slug)
    p = YoutubeBroadcastPreset.objects.create(
        name=payload.name,
        channel=channel,
        title_template=payload.title_template,
        description_template=payload.description_template,
        privacy=payload.privacy,
    )
    return {"id": p.id, "name": p.name}


# --------------------------------------------------------------------------- #
#  AudienceForm CRUD (studio admin)                                            #
# --------------------------------------------------------------------------- #


def _form_out(af) -> dict:
    from scheduling.models import AudienceSubmissionStatus

    total = af.submissions.filter(deleted_at__isnull=True).count()
    new_count = af.submissions.filter(
        deleted_at__isnull=True, status=AudienceSubmissionStatus.NEW
    ).count()
    return {
        "id": af.id,
        "kind": af.kind,
        "title": af.title,
        "description": af.description or "",
        "enabled": af.enabled,
        "requires_login": af.requires_login,
        "fields": af.fields or [],
        "success_message": af.success_message or "",
        "notify_email": af.notify_email or "",
        "starts_at": af.starts_at.isoformat() if af.starts_at else "",
        "ends_at": af.ends_at.isoformat() if af.ends_at else "",
        "prize": af.prize or "",
        "submission_count": total,
        "new_count": new_count,
    }


def _parse_dt(s: str):
    """ISO8601 文字列 → aware datetime (空は None)。"""
    from django.utils.dateparse import parse_datetime

    if not s:
        return None
    dt = parse_datetime(s)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _apply_form_payload(af, payload: AudienceFormIn) -> None:
    from scheduling.services import FieldSchemaError, validate_fields

    fields_list = [f.dict() for f in payload.fields]
    try:
        validate_fields(fields_list)
    except FieldSchemaError as exc:
        raise HttpError(422, str(exc)) from exc

    af.kind = payload.kind
    af.title = payload.title.strip()
    if not af.title:
        raise HttpError(422, "タイトルは必須です")
    af.description = payload.description
    af.enabled = payload.enabled
    af.requires_login = payload.requires_login
    af.fields = fields_list
    af.success_message = payload.success_message
    af.notify_email = payload.notify_email
    af.starts_at = _parse_dt(payload.starts_at)
    af.ends_at = _parse_dt(payload.ends_at)
    af.prize = payload.prize


@router.get(
    "/admin/scheduling/{slug}/series/{series_id}/forms",
    response=list[AudienceFormAdminOut],
)
def audience_forms_list(request: HttpRequest, slug: str, series_id: int):
    """シリーズに紐付く AudienceForm 一覧。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    forms = AudienceForm.objects.filter(series=series).prefetch_related("submissions")
    return [_form_out(af) for af in forms]


@router.post(
    "/admin/scheduling/{slug}/series/{series_id}/forms",
    response=AudienceFormAdminOut,
)
def audience_form_create(request: HttpRequest, slug: str, series_id: int, payload: AudienceFormIn):
    """フォーム定義を新規作成。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = AudienceForm(series=series)
    _apply_form_payload(af, payload)
    af.save()
    return _form_out(af)


@router.put(
    "/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}",
    response=AudienceFormAdminOut,
)
def audience_form_update(
    request: HttpRequest,
    slug: str,
    series_id: int,
    form_id: int,
    payload: AudienceFormIn,
):
    """フォーム定義を更新。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = get_object_or_404(AudienceForm, pk=form_id, series=series)
    _apply_form_payload(af, payload)
    af.save()
    return _form_out(af)


@router.delete(
    "/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}",
    response=OkOut,
)
def audience_form_delete(request: HttpRequest, slug: str, series_id: int, form_id: int):
    """フォーム定義を削除 (投稿も CASCADE 削除)。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = get_object_or_404(AudienceForm, pk=form_id, series=series)
    af.delete()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  AudienceSubmission 一覧 / 操作 / CSV                                        #
# --------------------------------------------------------------------------- #


def _sub_out(sub) -> dict:
    return {
        "id": sub.id,
        "form_id": sub.form_id,
        "form_title": sub.form.title,
        "member_id": sub.member_id,
        "submitter_name": sub.submitter_name or "",
        "submitter_email": sub.submitter_email or "",
        "status": sub.status,
        "payload": sub.payload or {},
        "created_at": sub.created_at.isoformat(),
        "deleted": sub.deleted_at is not None,
    }


@router.get(
    "/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}/submissions",
    response=list[AudienceSubmissionAdminOut],
)
def audience_submissions_list(
    request: HttpRequest,
    slug: str,
    series_id: int,
    form_id: int,
    status: str = "",
    include_deleted: bool = False,
):
    """投稿一覧。status 絞り込み / include_deleted で soft-delete 済みも含める。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, AudienceSubmission, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = get_object_or_404(AudienceForm, pk=form_id, series=series)
    qs = AudienceSubmission.objects.filter(form=af).select_related("form")
    if not include_deleted:
        qs = qs.filter(deleted_at__isnull=True)
    if status:
        qs = qs.filter(status=status)
    return [_sub_out(s) for s in qs.order_by("-created_at")[:500]]


@router.post(
    "/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}/submissions/{sub_id}",
    response=OkOut,
)
def audience_submission_update(
    request: HttpRequest,
    slug: str,
    series_id: int,
    form_id: int,
    sub_id: int,
    action: str,  # "read" | "handled" | "delete" | "restore"
):
    """投稿の status 変更 / soft-delete / 復元。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, AudienceSubmission, AudienceSubmissionStatus, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = get_object_or_404(AudienceForm, pk=form_id, series=series)
    sub = get_object_or_404(AudienceSubmission, pk=sub_id, form=af)

    if action == "read":
        sub.status = AudienceSubmissionStatus.READ
        sub.save(update_fields=["status"])
    elif action == "handled":
        sub.status = AudienceSubmissionStatus.HANDLED
        sub.save(update_fields=["status"])
    elif action == "delete":
        sub.deleted_at = timezone.now()
        sub.save(update_fields=["deleted_at"])
    elif action == "restore":
        sub.deleted_at = None
        sub.save(update_fields=["deleted_at"])
    else:
        raise HttpError(400, f"不正な action: {action}")
    return {"ok": True}


@router.get(
    "/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}/submissions.csv",
)
def audience_submissions_csv(request: HttpRequest, slug: str, series_id: int, form_id: int):
    """投稿を CSV でストリーミングダウンロード (deleted 除く)。"""
    from core.models import Channel
    from scheduling.models import AudienceForm, AudienceSubmission, Series

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    af = get_object_or_404(AudienceForm, pk=form_id, series=series)

    field_keys = [f["key"] for f in (af.fields or [])]
    field_labels = {f["key"]: f.get("label", f["key"]) for f in (af.fields or [])}

    header = ["ID", "日時", "ステータス", "投稿者名", "メール"] + [
        field_labels.get(k, k) for k in field_keys
    ]

    def gen():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        buf.truncate(0)
        buf.seek(0)

        for sub in (
            AudienceSubmission.objects.filter(form=af, deleted_at__isnull=True)
            .order_by("-created_at")
            .iterator(chunk_size=200)
        ):
            local_dt = timezone.localtime(sub.created_at).strftime("%Y-%m-%d %H:%M:%S")
            row = [
                sub.id,
                local_dt,
                sub.get_status_display(),
                sub.submitter_name,
                sub.submitter_email,
            ] + [str(sub.payload.get(k, "")) for k in field_keys]
            # 投稿者名 / メール / 自由入力欄はすべて公開フォーム由来の未検証入力なので、
            # 表計算で開いたときに数式として評価されないよう無害化する (#sec L-9)。
            # ヘッダは運用者が定義した列名なので対象外。
            writer.writerow([escape_formula(v) for v in row])
            yield buf.getvalue()
            buf.truncate(0)
            buf.seek(0)

    filename = f"submissions_{form_id}.csv"
    response = StreamingHttpResponse(gen(), content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --------------------------------------------------------------------------- #
#  SeriesPost CRUD (studio admin, #27 ファンクラブ限定レベル設定込み)          #
# --------------------------------------------------------------------------- #


def _post_out(post) -> dict:
    return {
        "id": post.id,
        "kind": post.kind,
        "title": post.title,
        "body": post.body,
        "media_url": post.media_url,
        "form_url": post.form_url,
        "campaign_start": post.campaign_start.isoformat() if post.campaign_start else "",
        "campaign_end": post.campaign_end.isoformat() if post.campaign_end else "",
        "is_published": post.is_published,
        "published_at": post.published_at.isoformat() if post.published_at else "",
        "fc_required_level": post.fc_required_level,
    }


def _apply_post_payload(post, payload: SeriesPostIn) -> None:
    post.kind = payload.kind
    post.title = payload.title.strip()
    if not post.title:
        raise HttpError(422, "タイトルは必須です")
    post.body = payload.body
    post.media_url = payload.media_url
    post.form_url = payload.form_url
    post.campaign_start = _parse_dt(payload.campaign_start)
    post.campaign_end = _parse_dt(payload.campaign_end)
    was_published = post.is_published
    post.is_published = payload.is_published
    if payload.is_published and not was_published:
        post.published_at = timezone.now()
    post.fc_required_level = payload.fc_required_level


@router.get(
    "/admin/scheduling/{slug}/series/{series_id}/posts",
    response=list[SeriesPostAdminOut],
)
def series_posts_list(request: HttpRequest, slug: str, series_id: int):
    """シリーズに紐付く SeriesPost(ブログ/キャンペーン) 一覧。"""
    from core.models import Channel
    from scheduling.models import Series, SeriesPost

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    posts = SeriesPost.objects.filter(series=series).order_by("-created_at")
    return [_post_out(p) for p in posts]


@router.post(
    "/admin/scheduling/{slug}/series/{series_id}/posts",
    response=SeriesPostAdminOut,
)
def series_post_create(request: HttpRequest, slug: str, series_id: int, payload: SeriesPostIn):
    from core.models import Channel
    from scheduling.models import Series, SeriesPost

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    post = SeriesPost(series=series)
    _apply_post_payload(post, payload)
    post.save()
    return _post_out(post)


@router.put(
    "/admin/scheduling/{slug}/series/{series_id}/posts/{post_id}",
    response=SeriesPostAdminOut,
)
def series_post_update(
    request: HttpRequest, slug: str, series_id: int, post_id: int, payload: SeriesPostIn
):
    from core.models import Channel
    from scheduling.models import Series, SeriesPost

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series)
    _apply_post_payload(post, payload)
    post.save()
    return _post_out(post)


@router.delete(
    "/admin/scheduling/{slug}/series/{series_id}/posts/{post_id}",
    response=OkOut,
)
def series_post_delete(request: HttpRequest, slug: str, series_id: int, post_id: int):
    from core.models import Channel
    from scheduling.models import Series, SeriesPost

    channel = get_object_or_404(Channel, slug=slug)
    series = get_object_or_404(Series, pk=series_id, channel=channel)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series)
    post.delete()
    return {"ok": True}
