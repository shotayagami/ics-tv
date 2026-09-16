# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材・CM 管理 (#7 / docs/ui.md「素材・CM」)。staff 専用。

Django admin の生 CRUD は運用に不適 (尺・正規化状態・CM 在庫・考査が一覧で読めない) なため、
normalize_status バッジ・CM 在庫状態・考査ワンクリックを備えた専用画面を提供する。
素材・CM は ch 非依存 (全 ch 共有) なのでヘッダ ch 選択は戻りナビ用途のみ。
素材編集・CM 登録/編集・CM束・フィラーの編集も専用画面で提供する (Django admin 直編集を置換)。
"""

from __future__ import annotations

import contextlib
from datetime import date

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from core import r2, thumbnails
from core.models import Channel
from core.utils import move_ordered_item
from medialib import forms as ml_forms
from medialib import services
from medialib.models import (
    Asset,
    AssetKind,
    CmBundle,
    CmBundleItem,
    CmCreative,
    CmGrid,
    CueKind,
    CuePoint,
    CueSheet,
    FillerItem,
    FillerPlaylist,
    NormalizeStatus,
    ScreeningStatus,
)


def _shell_ctx(request) -> dict:
    """ch 非依存画面でも共通シェル (ヘッダ ch 選択・編成/運行への戻りナビ) を成立させる。"""
    channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    current_slug = request.session.get("current_ch") or (channels[0].slug if channels else None)
    return {"channels": channels, "current_ch_slug": current_slug, "active": "medialib"}


def _ok(message: str, trigger: str) -> HttpResponse:
    return HttpResponse(message, headers={"HX-Trigger": trigger})


@staff_member_required
def dashboard(request) -> HttpResponse:
    sel_kind = request.GET.get("kind") or ""
    sel_status = request.GET.get("status") or ""

    assets_qs = Asset.objects.all().order_by("-created_at")
    if sel_kind:
        assets_qs = assets_qs.filter(kind=sel_kind)
    if sel_status:
        assets_qs = assets_qs.filter(normalize_status=sel_status)
    assets = list(assets_qs[:200])

    today = date.today()
    creatives = CmCreative.objects.select_related("asset").order_by("-asset__created_at")[:200]
    cm_rows = []
    n_screening_pending = 0
    for c in creatives:
        cm_rows.append({"cm": c, "stock": services.cm_stock_state(c, today)})
        if c.screening_status == ScreeningStatus.PENDING:
            n_screening_pending += 1

    cue_asset_ids = set(
        CueSheet.objects.filter(asset__in=[a.id for a in assets]).values_list("asset_id", flat=True)
    )

    bundles = list(CmBundle.objects.annotate(n=Count("items")).order_by("name"))
    fillers = list(FillerPlaylist.objects.annotate(n=Count("items")).order_by("name"))

    ctx = _shell_ctx(request)
    ctx.update(
        {
            "assets": assets,
            "cm_rows": cm_rows,
            "bundles": bundles,
            "fillers": fillers,
            "kinds": AssetKind.choices,
            "statuses": NormalizeStatus.choices,
            "sel_kind": sel_kind,
            "sel_status": sel_status,
            "n_screening_pending": n_screening_pending,
            "cue_asset_ids": cue_asset_ids,
        }
    )
    return render(request, "medialib/dashboard.html", ctx)


@staff_member_required
@require_POST
def op_renormalize(request, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(Asset, pk=asset_id)
    services.renormalize(asset)
    return _ok(f"「{asset.title}」を正規化キューに投入しました", "assetRenormalized")


@staff_member_required
@require_POST
def op_screening(request, asset_id: int) -> HttpResponse:
    cm = get_object_or_404(CmCreative, pk=asset_id)
    try:
        status = services.set_screening(cm, request.POST.get("decision", ""))
    except ValueError as e:
        return HttpResponse(str(e), status=400)
    return _ok(f"考査を更新しました ({cm.advertiser} → {status.label})", "screeningUpdated")


# ---- キューシート編集 (素材内部ランダウン) ----


def _ms_to_minsec(ms: int | None) -> str:
    if ms is None:
        return "—"
    total = ms // 1000
    return f"{total // 60}:{total % 60:02d}"


def _minsec_to_ms(minutes: str, seconds: str, millis: str = "0") -> int:
    return (int(minutes or 0) * 60 + int(seconds or 0)) * 1000 + int(millis or 0)


@staff_member_required
def cuesheet_editor(request, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(Asset, pk=asset_id)
    cue = CueSheet.objects.filter(asset=asset).first()
    points = list(cue.points.all()) if cue else []

    # ad_break 行に「素材内オフセット (= それ以前の本編累積)」を付して、CM の落ち位置を可視化
    content_head = 0
    rows = []
    for p in points:
        offset = None
        if p.kind == CueKind.CONTENT:
            content_head += p.duration_ms
        else:
            offset = _ms_to_minsec(content_head)
        rows.append({"p": p, "dur": _ms_to_minsec(p.duration_ms), "offset": offset})

    content_total = sum(p.duration_ms for p in points if p.kind == CueKind.CONTENT)
    error = None
    if cue and points:
        try:
            services.validate_cue_sheet(cue)
        except services.CueSheetError as e:
            error = str(e)

    # 残り本編尺 (素材尺 − 本編合計)。端数 ms を「残尺充填」ボタン/ms 入力で合わせるための補助。
    remaining_ms = remaining_min = remaining_sec = remaining_ms_only = 0
    remaining_disp = ""
    if asset.duration_ms is not None:
        rem = asset.duration_ms - content_total
        if rem > 0:
            remaining_ms = rem
            remaining_min, r = divmod(rem, 60000)
            remaining_sec, remaining_ms_only = divmod(r, 1000)
            remaining_disp = f"{remaining_min}:{remaining_sec:02d}.{remaining_ms_only:03d}"

    ctx = _shell_ctx(request)
    ctx.update(
        {
            "asset": asset,
            "cue": cue,
            "rows": rows,
            "grids": CmGrid.choices,
            "asset_duration_disp": asset.duration_display,
            "content_total_disp": _ms_to_minsec(content_total) if points else "0:00",
            "airtime_disp": (
                _ms_to_minsec(services.cue_total_airtime_ms(cue)) if cue and points else "—"
            ),
            "valid_error": error,
            "is_valid": cue is not None and points and error is None,
            "remaining_ms": remaining_ms,
            "remaining_min": remaining_min,
            "remaining_sec": remaining_sec,
            "remaining_ms_only": remaining_ms_only,
            "remaining_disp": remaining_disp,
        }
    )
    return render(request, "medialib/cuesheet.html", ctx)


@staff_member_required
@require_POST
def cuepoint_add(request, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(Asset, pk=asset_id)
    kind = request.POST.get("kind")
    if kind not in CueKind.values:
        return HttpResponse("種別が不正です", status=400)
    try:
        duration_ms = _minsec_to_ms(
            request.POST.get("min", "0"),
            request.POST.get("sec", "0"),
            request.POST.get("ms", "0"),
        )
    except ValueError:
        return HttpResponse("尺が不正です", status=400)
    if duration_ms <= 0:
        return HttpResponse("尺は正の値が必要です", status=400)
    grid = request.POST.get("grid") if kind == CueKind.AD_BREAK else None
    if kind == CueKind.AD_BREAK and grid not in CmGrid.values:
        return HttpResponse("CM枠には grid (15s/20s) が必要です", status=400)

    cue, _ = CueSheet.objects.get_or_create(asset=asset)
    next_seq = (cue.points.aggregate(m=Max("seq"))["m"] or 0) + 1
    CuePoint.objects.create(
        cue_sheet=cue,
        seq=next_seq,
        kind=kind,
        duration_ms=duration_ms,
        grid=grid,
        label=request.POST.get("label", "").strip() or None,
    )
    return _ok("行を追加しました", "cueChanged")


@staff_member_required
@require_POST
def cuepoint_delete(request, point_id: int) -> HttpResponse:
    point = get_object_or_404(CuePoint, pk=point_id)
    point.delete()
    return _ok("行を削除しました", "cueChanged")


@staff_member_required
@require_POST
def cuepoint_move(request, point_id: int) -> HttpResponse:
    """上下の隣接行と seq を入れ替える (direction=up|down)。"""
    point = get_object_or_404(CuePoint.objects.select_related("cue_sheet"), pk=point_id)
    direction = request.POST.get("direction")
    if direction not in ("up", "down"):
        return HttpResponse("direction が不正です", status=400)
    move_ordered_item(point, point.cue_sheet.points, direction)
    return _ok("並べ替えました", "cueChanged")


# ---- 素材・CM・束・フィラーの専用編集 (#7、Django admin 直編集を置換) ----


@staff_member_required
@require_http_methods(["GET", "POST"])
def asset_edit(request, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(Asset, pk=asset_id)
    if request.method == "POST":
        form = ml_forms.AssetForm(request.POST, instance=asset)
        if form.is_valid():
            obj = form.save(commit=False)
            try:
                up = thumbnails.apply_upload(request)
            except thumbnails.ThumbnailError as e:
                form.add_error(None, str(e))
            else:
                if up:
                    obj.thumbnail_url = up
                obj.save()
                return redirect("medialib:dashboard")
    else:
        form = ml_forms.AssetForm(instance=asset)
    # 正規化後 mezzanine の署名付きプレビュー URL を埋める (R2 未設定でも 500 しない best-effort)。
    preview_url_value = ""
    if asset.r2_key:
        with contextlib.suppress(Exception):
            preview_url_value = r2.presign_get(asset.r2_key)
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "asset": asset, "preview_url": preview_url_value})
    return render(request, "medialib/asset_form.html", ctx)


@staff_member_required
def asset_preview_url(request, asset_id: int) -> HttpResponse:
    """正規化済み mezzanine (R2) の期限付き署名 GET URL を返す (HTML5 video プレビュー失効時の再取得用)。"""
    asset = get_object_or_404(Asset, pk=asset_id)
    if not asset.r2_key:
        return HttpResponse("正規化後の mezzanine がありません", status=404)
    return JsonResponse({"url": r2.presign_get(asset.r2_key)})


@staff_member_required
@require_http_methods(["GET", "POST"])
def cm_new(request) -> HttpResponse:
    # CmCreative 未設定の kind=cm 素材に CM 属性を付ける (素材自体は納品/正規化で入る)
    free_assets = list(
        Asset.objects.filter(kind=AssetKind.CM, cm__isnull=True).order_by("-created_at")
    )
    if request.method == "POST":
        asset = get_object_or_404(Asset, pk=request.POST.get("asset_id"), kind=AssetKind.CM)
        form = ml_forms.CmCreativeForm(request.POST)
        if form.is_valid():
            try:
                up = thumbnails.apply_upload(request)
            except thumbnails.ThumbnailError as e:
                form.add_error(None, str(e))
            else:
                cm = form.save(commit=False)
                cm.asset = asset
                cm.save()
                asset.thumbnail_url = up or form.cleaned_data.get("thumbnail_url") or None
                asset.save(update_fields=["thumbnail_url"])
                return redirect("medialib:dashboard")
    else:
        form = ml_forms.CmCreativeForm()
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "free_assets": free_assets, "is_new": True})
    return render(request, "medialib/cm_form.html", ctx)


@staff_member_required
@require_http_methods(["GET", "POST"])
def cm_edit(request, asset_id: int) -> HttpResponse:
    cm = get_object_or_404(CmCreative.objects.select_related("asset"), pk=asset_id)
    if request.method == "POST":
        form = ml_forms.CmCreativeForm(request.POST, instance=cm)
        if form.is_valid():
            try:
                up = thumbnails.apply_upload(request)
            except thumbnails.ThumbnailError as e:
                form.add_error(None, str(e))
            else:
                form.save()
                cm.asset.thumbnail_url = up or form.cleaned_data.get("thumbnail_url") or None
                cm.asset.save(update_fields=["thumbnail_url"])
                return redirect("medialib:dashboard")
    else:
        form = ml_forms.CmCreativeForm(instance=cm)
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "cm": cm, "is_new": False})
    return render(request, "medialib/cm_form.html", ctx)


@staff_member_required
@require_http_methods(["GET", "POST"])
def bundle_new(request) -> HttpResponse:
    if request.method == "POST":
        form = ml_forms.CmBundleForm(request.POST)
        if form.is_valid():
            b = form.save()
            return redirect("medialib:bundle_edit", bundle_id=b.id)
    else:
        form = ml_forms.CmBundleForm()
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "is_new": True})
    return render(request, "medialib/bundle_form.html", ctx)


@staff_member_required
@require_http_methods(["GET", "POST"])
def bundle_edit(request, bundle_id: int) -> HttpResponse:
    bundle = get_object_or_404(CmBundle, pk=bundle_id)
    if request.method == "POST":
        form = ml_forms.CmBundleForm(request.POST, instance=bundle)
        if form.is_valid():
            form.save()
            return redirect("medialib:bundle_edit", bundle_id=bundle.id)
    else:
        form = ml_forms.CmBundleForm(instance=bundle)
    items = list(bundle.items.select_related("cm_asset__asset").order_by("seq"))
    cms = list(CmCreative.objects.select_related("asset").order_by("advertiser"))
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "bundle": bundle, "items": items, "cms": cms, "is_new": False})
    return render(request, "medialib/bundle_form.html", ctx)


@staff_member_required
@require_POST
def bundle_item_add(request, bundle_id: int) -> HttpResponse:
    bundle = get_object_or_404(CmBundle, pk=bundle_id)
    cm = get_object_or_404(CmCreative, pk=request.POST.get("cm_asset_id"))
    next_seq = (bundle.items.aggregate(m=Max("seq"))["m"] or 0) + 1
    CmBundleItem.objects.create(cm_bundle=bundle, seq=next_seq, cm_asset=cm)
    return _ok("CM を追加しました", "bundleChanged")


@staff_member_required
@require_POST
def bundle_item_delete(request, item_id: int) -> HttpResponse:
    get_object_or_404(CmBundleItem, pk=item_id).delete()
    return _ok("削除しました", "bundleChanged")


@staff_member_required
@require_POST
def bundle_item_move(request, item_id: int) -> HttpResponse:
    item = get_object_or_404(CmBundleItem.objects.select_related("cm_bundle"), pk=item_id)
    move_ordered_item(item, item.cm_bundle.items, request.POST.get("direction", ""))
    return _ok("並べ替えました", "bundleChanged")


@staff_member_required
@require_http_methods(["GET", "POST"])
def filler_new(request) -> HttpResponse:
    if request.method == "POST":
        form = ml_forms.FillerPlaylistForm(request.POST)
        if form.is_valid():
            p = form.save()
            return redirect("medialib:filler_edit", playlist_id=p.id)
    else:
        form = ml_forms.FillerPlaylistForm()
    ctx = _shell_ctx(request)
    ctx.update({"form": form, "is_new": True})
    return render(request, "medialib/filler_form.html", ctx)


@staff_member_required
@require_http_methods(["GET", "POST"])
def filler_edit(request, playlist_id: int) -> HttpResponse:
    playlist = get_object_or_404(FillerPlaylist, pk=playlist_id)
    if request.method == "POST":
        form = ml_forms.FillerPlaylistForm(request.POST, instance=playlist)
        if form.is_valid():
            form.save()
            return redirect("medialib:filler_edit", playlist_id=playlist.id)
    else:
        form = ml_forms.FillerPlaylistForm(instance=playlist)
    items = list(playlist.items.select_related("asset").order_by("seq"))
    # フィラー候補: usable_as_filler の素材 (既定は filler/bumper/slate。番組素材も編集画面でフラグを立てれば対象)
    assets = list(Asset.objects.filter(usable_as_filler=True).order_by("title"))
    ctx = _shell_ctx(request)
    ctx.update(
        {"form": form, "playlist": playlist, "items": items, "assets": assets, "is_new": False}
    )
    return render(request, "medialib/filler_form.html", ctx)


@staff_member_required
@require_POST
def filler_item_add(request, playlist_id: int) -> HttpResponse:
    playlist = get_object_or_404(FillerPlaylist, pk=playlist_id)
    asset = get_object_or_404(Asset, pk=request.POST.get("asset_id"))
    next_seq = (playlist.items.aggregate(m=Max("seq"))["m"] or 0) + 1
    FillerItem.objects.create(filler_playlist=playlist, seq=next_seq, asset=asset)
    return _ok("素材を追加しました", "fillerChanged")


@staff_member_required
@require_POST
def filler_item_delete(request, item_id: int) -> HttpResponse:
    get_object_or_404(FillerItem, pk=item_id).delete()
    return _ok("削除しました", "fillerChanged")


@staff_member_required
@require_POST
def filler_item_move(request, item_id: int) -> HttpResponse:
    item = get_object_or_404(FillerItem.objects.select_related("filler_playlist"), pk=item_id)
    move_ordered_item(item, item.filler_playlist.items, request.POST.get("direction", ""))
    return _ok("並べ替えました", "fillerChanged")
