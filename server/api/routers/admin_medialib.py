# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-6): 素材ライブラリ medialib ダッシュボード。staff 限定。

素材一覧 (種別/正規化状態フィルタ) + CM 在庫/考査 + バンドル/フィラーの概観。既存
medialib.views.dashboard と同じ集計を JSON 化。再正規化/考査の操作は既存エンドポイント
(/medialib/asset/<id>/renormalize/・/medialib/cm/<id>/screening/) を SPA から再利用。各エディタ
(asset_edit/cm/bundle/filler/cuesheet) は据え置きで edit_url から旧画面へ。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from django.http import HttpRequest
from ninja import Header, Query, Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import (
    CueSheetOut,
    MedialibOut,
    MediaUploadCompleteIn,
    MediaUploadOut,
    MediaUploadStartIn,
)

router = Router(tags=["admin"], auth=staff_auth)


def _require_upload_permission(request: HttpRequest) -> None:
    if not request.user.has_perm("medialib.add_asset"):
        raise HttpError(403, "素材追加権限が必要です")


def _upload_out(upload, parts: list[dict] | None = None) -> dict:
    from medialib.upload_services import PART_SIZE_BYTES

    return {
        "upload_uuid": str(upload.id),
        "status": upload.status,
        "part_size_bytes": PART_SIZE_BYTES,
        "parts": parts or [],
        "expires_at": upload.expires_at.isoformat(),
        "asset_id": upload.asset_id,
        "error_code": upload.last_error_code or "",
    }


@router.post("/admin/media-uploads/start", response=MediaUploadOut)
def media_upload_start(
    request: HttpRequest,
    payload: MediaUploadStartIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
):
    from medialib import upload_services

    _require_upload_permission(request)
    try:
        upload, parts = upload_services.start_upload(
            owner=request.user,
            idempotency_key=idempotency_key,
            **payload.model_dump(),
        )
    except upload_services.UploadValidationError as exc:
        raise HttpError(422, str(exc)) from exc
    except upload_services.UploadConflictError as exc:
        raise HttpError(409, str(exc)) from exc
    except upload_services.UploadStorageUnavailableError as exc:
        raise HttpError(503, "upload storage is unavailable") from exc
    return _upload_out(upload, parts)


@router.post("/admin/media-uploads/{upload_id}/complete", response=MediaUploadOut)
def media_upload_complete(
    request: HttpRequest,
    upload_id: UUID,
    payload: MediaUploadCompleteIn,
):
    from medialib import upload_services

    _require_upload_permission(request)
    try:
        upload = upload_services.complete_upload(
            owner=request.user,
            upload_id=upload_id,
            parts=[part.model_dump() for part in payload.parts],
        )
    except upload_services.UploadValidationError as exc:
        raise HttpError(422, str(exc)) from exc
    except upload_services.UploadConflictError as exc:
        raise HttpError(409, str(exc)) from exc
    except upload_services.UploadStorageUnavailableError as exc:
        raise HttpError(503, "could not inspect multipart upload") from exc
    except upload_services.AssetUpload.DoesNotExist as exc:
        raise HttpError(404, "upload not found") from exc
    return _upload_out(upload)


@router.post("/admin/media-uploads/{upload_id}/abort", response=MediaUploadOut)
def media_upload_abort(request: HttpRequest, upload_id: UUID):
    from medialib import upload_services

    _require_upload_permission(request)
    try:
        upload = upload_services.abort_upload(owner=request.user, upload_id=upload_id)
    except upload_services.UploadConflictError as exc:
        raise HttpError(409, str(exc)) from exc
    except upload_services.AssetUpload.DoesNotExist as exc:
        raise HttpError(404, "upload not found") from exc
    return _upload_out(upload)


@router.get("/admin/media-uploads/{upload_id}", response=MediaUploadOut)
def media_upload_status(request: HttpRequest, upload_id: UUID):
    from medialib.models import AssetUpload

    _require_upload_permission(request)
    # staff_auth が認証済 (request.user は staff User)
    upload = AssetUpload.objects.filter(pk=upload_id, owner_id=request.user.pk).first()
    if upload is None:
        raise HttpError(404, "upload not found")
    return _upload_out(upload)


def _disp(ms: int) -> str:
    m, r = divmod(ms, 60000)
    s, msec = divmod(r, 1000)
    return f"{m}:{s:02d}.{msec:03d}"


@router.get("/admin/medialib/asset/{int:asset_id}/cuesheet", response=CueSheetOut)
def cuesheet(request: HttpRequest, asset_id: int):
    """キューシート (本編 ⊕ CM枠 の順序リスト)。素材内オフセットと残尺・検証エラーを返す。"""
    from django.shortcuts import get_object_or_404

    from medialib import services
    from medialib.models import Asset, CueKind, CueSheet

    asset = get_object_or_404(Asset, pk=asset_id)
    cue = CueSheet.objects.filter(asset=asset).first()
    points = list(cue.points.all()) if cue else []

    content_head = 0
    rows = []
    for p in points:
        offset = ""
        if p.kind == CueKind.CONTENT:
            content_head += p.duration_ms
        else:
            offset = _disp(content_head)
        rows.append(
            {
                "id": p.id,
                "kind": p.kind,
                "kind_label": p.get_kind_display(),
                "duration": _disp(p.duration_ms),
                "offset": offset,
                "grid": getattr(p, "grid", "") or "",
            }
        )

    content_total = sum(p.duration_ms for p in points if p.kind == CueKind.CONTENT)
    error = ""
    if cue and points:
        try:
            services.validate_cue_sheet(cue)
        except services.CueSheetError as e:
            error = str(e)

    remaining_ms = 0
    remaining_disp = ""
    if asset.duration_ms is not None:
        rem = asset.duration_ms - content_total
        if rem > 0:
            remaining_ms = rem
            remaining_disp = _disp(rem)

    return {
        "asset_id": asset.id,
        "asset_title": asset.title,
        "asset_duration": asset.duration_display,
        "points": rows,
        "content_total": _disp(content_total),
        "remaining_ms": remaining_ms,
        "remaining_disp": remaining_disp,
        "error": error,
    }


def _camp(d) -> str:
    return d.strftime("%y/%m/%d") if d else "?"


@router.get("/admin/medialib/dashboard", response=MedialibOut)
def medialib_dashboard(
    request: HttpRequest,
    kind: str = Query(""),
    status: str = Query(""),
):
    from django.db.models import Count

    from core.models import Channel
    from medialib import services
    from medialib.models import (
        Asset,
        AssetKind,
        CmBundle,
        CmCreative,
        CueSheet,
        FillerItem,
        FillerPlaylist,
        NormalizeStatus,
        ScreeningStatus,
    )
    from scheduling.models import Program

    qs = Asset.objects.all().order_by("-created_at")
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(normalize_status=status)
    assets = list(qs[:200])
    asset_ids = [a.id for a in assets]

    cue_asset_ids = set(
        CueSheet.objects.filter(asset__in=asset_ids).values_list("asset_id", flat=True)
    )

    # --- グループ算出 ---
    # 1) シリーズ: program asset → Program → Series
    asset_group: dict[int, tuple[str, str]] = {}
    for srow in (
        Program.objects.filter(asset__in=asset_ids)
        .exclude(series__isnull=True)
        .select_related("series")
        .values("asset_id", "series__id", "series__title")
        .distinct()
    ):
        sid, stitle = srow["series__id"], srow["series__title"] or "シリーズ不明"
        asset_group.setdefault(srow["asset_id"], (f"series_{sid}", stitle))

    # 2) CM: CmCreative.advertiser
    for crow in CmCreative.objects.filter(asset__in=asset_ids).values("asset_id", "advertiser"):
        adv = crow["advertiser"] or "広告主不明"
        asset_group.setdefault(crow["asset_id"], (f"cm_{adv}", f"CM：{adv}"))

    # 3) フィラー: FillerItem → FillerPlaylist → Channel.default_filler
    playlist_to_asset_ids: dict[int, list[int]] = {}
    for frow in FillerItem.objects.filter(asset__in=asset_ids).values(
        "asset_id", "filler_playlist_id"
    ):
        playlist_to_asset_ids.setdefault(frow["filler_playlist_id"], []).append(frow["asset_id"])
    if playlist_to_asset_ids:
        for ch in Channel.objects.filter(default_filler__in=playlist_to_asset_ids.keys()).values(
            "default_filler_id", "slug", "name"
        ):
            for aid in playlist_to_asset_ids.get(ch["default_filler_id"], []):
                asset_group.setdefault(aid, (f"ch_{ch['slug']}", f"フィラー：{ch['name']}"))
        # フィラーPL に属するが CH 未割当のものはプレイリスト名でまとめる
        pl_names = {
            p.id: p.name for p in FillerPlaylist.objects.filter(id__in=playlist_to_asset_ids.keys())
        }
        for pl_id, aids in playlist_to_asset_ids.items():
            pl_label = f"フィラー：{pl_names.get(pl_id, str(pl_id))}"
            for aid in aids:
                asset_group.setdefault(aid, (f"pl_{pl_id}", pl_label))

    today = date.today()
    cms = []
    n_pending = 0
    for c in CmCreative.objects.select_related("asset").order_by("-asset__created_at")[:200]:
        stock = services.cm_stock_state(c, today)
        if c.screening_status == ScreeningStatus.PENDING:
            n_pending += 1
        camp = "—"
        if c.campaign_start or c.campaign_end:
            camp = f"{_camp(c.campaign_start)}–{_camp(c.campaign_end)}"
        cms.append(
            {
                "asset_id": c.asset_id,
                "advertiser": c.advertiser,
                "grid": c.get_grid_display(),
                "campaign": camp,
                "aired": f"{c.aired_count}/{c.max_airings}"
                if c.max_airings
                else f"{c.aired_count}/∞",
                "stock_label": stock["label"],
                "stock_css": stock["css"],
                "screening_status": c.screening_status,
                "edit_url": f"/medialib/cm/{c.asset_id}/edit/",
            }
        )

    bundles = [
        {"id": b.id, "name": b.name, "count": b.n, "edit_url": f"/medialib/bundle/{b.id}/edit/"}
        for b in CmBundle.objects.annotate(n=Count("items")).order_by("name")
    ]
    fillers = [
        {"id": f.id, "name": f.name, "count": f.n, "edit_url": f"/medialib/filler/{f.id}/edit/"}
        for f in FillerPlaylist.objects.annotate(n=Count("items")).order_by("name")
    ]

    return {
        "assets": [
            {
                "id": a.id,
                "title": a.title,
                "kind": a.kind,
                "duration_display": a.duration_display,
                "thumbnail_url": a.thumbnail_url or "",
                "normalize_status": a.normalize_status,
                "has_cuesheet": a.id in cue_asset_ids,
                "is_program": a.kind == AssetKind.PROGRAM,
                "edit_url": f"/medialib/asset/{a.id}/edit/",
                "cuesheet_url": f"/medialib/asset/{a.id}/cuesheet/",
                "group_key": asset_group.get(a.id, ("other", "その他"))[0],
                "group_label": asset_group.get(a.id, ("other", "その他"))[1],
            }
            for a in assets
        ],
        "cms": cms,
        "bundles": bundles,
        "fillers": fillers,
        "kinds": [{"value": v, "label": label} for v, label in AssetKind.choices],
        "statuses": [{"value": v, "label": label} for v, label in NormalizeStatus.choices],
        "sel_kind": kind,
        "sel_status": status,
        "n_screening_pending": n_pending,
    }
