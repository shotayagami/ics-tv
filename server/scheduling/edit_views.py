# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""編成タイムラインの編集 API (docs/ui.md「編成タイムライン」/ Phase 2)。

ドラッグ移動 (start_at 変更・尺保持)・リサイズ (live の end_at)・配置検証 (EXCLUSION)・
ad_break グラフィック編集 (offset_ms / grid 整数倍)。重なり禁止は program の EXCLUDE 制約が正で、
本 API は事前チェックで 422 + 配置可能な最早時刻を返し、保存は atomic + IntegrityError で二重に守る。
編集後は当該 ch を即時再解決して playout_event を追従させる。
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import dateparse, timezone
from django.views.decorators.http import require_POST

from core.models import Channel
from core.utils import move_ordered_item
from scheduling.forms import SeriesSlotForm
from scheduling.models import (
    AdBreak,
    CmGrid,
    LiveCue,
    LiveCueAnchor,
    LiveCueKind,
    LiveCueState,
    LiveRundown,
    LiveRundownTemplate,
    LiveRundownTemplateCue,
    Program,
    ProgramType,
    Series,
    SeriesSlot,
)


def _parse_dt(s: str | None):
    if not s:
        return None
    dt = dateparse.parse_datetime(s)
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _overlaps(channel, start, end, exclude_id) -> bool:
    return (
        Program.objects.filter(channel=channel, start_at__lt=end, end_at__gt=start)
        .exclude(pk=exclude_id)
        .exists()
    )


def _earliest_free(channel, duration, after, exclude_id):
    """[t, t+duration) が空く最早 t (>= after)。他番組を時刻順に走査し最初の十分なギャップ。"""
    progs = (
        Program.objects.filter(channel=channel, end_at__gt=after)
        .exclude(pk=exclude_id)
        .order_by("start_at")
    )
    t = after
    for p in progs:
        if p.end_at <= t:
            continue
        if p.start_at - t >= duration:  # この番組の前に収まる
            return t
        t = p.end_at
    return t


def _resolve_soon(channel_id: int) -> None:
    from scheduling.tasks import resolve_channel_now

    transaction.on_commit(lambda: resolve_channel_now.delay(channel_id))


@staff_member_required
@require_POST
def program_validate(request, slug: str):
    """ドラッグ中の配置検証 (DB 更新なし)。422 で最早配置可能時刻を返す (ui.md 状態B)。"""
    channel = get_object_or_404(Channel, slug=slug)
    data = _body(request)
    prog = get_object_or_404(Program, pk=data.get("program_id"), channel=channel)
    start = _parse_dt(data.get("start_at"))
    if start is None:
        return JsonResponse({"ok": False, "error": "start_at が不正"}, status=400)
    duration = prog.end_at - prog.start_at
    end = start + duration
    if _overlaps(channel, start, end, prog.id):
        earliest = _earliest_free(channel, duration, start, prog.id)
        return JsonResponse({"ok": False, "earliest": earliest.isoformat()}, status=422)
    return JsonResponse({"ok": True, "start_at": start.isoformat(), "end_at": end.isoformat()})


@staff_member_required
@require_POST
def program_move(request, slug: str, program_id: int):
    """start_at をドラッグ先へ変更し尺を保持 (録画/生とも移動は尺固定。ui.md)。"""
    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(Program, pk=program_id, channel=channel)
    start = _parse_dt(_body(request).get("start_at"))
    if start is None:
        return JsonResponse({"ok": False, "error": "start_at が不正"}, status=400)
    duration = prog.end_at - prog.start_at
    end = start + duration
    if _overlaps(channel, start, end, prog.id):
        earliest = _earliest_free(channel, duration, start, prog.id)
        return JsonResponse(
            {"ok": False, "error": "overlap", "earliest": earliest.isoformat()}, status=422
        )
    try:
        with transaction.atomic():
            prog.start_at, prog.end_at = start, end
            prog.save(update_fields=["start_at", "end_at"])
            _resolve_soon(channel.id)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "overlap"}, status=422)
    return JsonResponse({"ok": True, "start_at": start.isoformat(), "end_at": end.isoformat()})


@staff_member_required
@require_POST
def program_resize(request, slug: str, program_id: int):
    """end_at を変更 (live のみ。録画は asset 尺 + ad_break で自動算出のため不可。ui.md)。"""
    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(Program, pk=program_id, channel=channel)
    if prog.type != ProgramType.LIVE:
        return JsonResponse(
            {"ok": False, "error": "録画番組の尺は素材尺で固定 (リサイズ不可)"}, status=422
        )
    end = _parse_dt(_body(request).get("end_at"))
    if end is None or end <= prog.start_at:
        return JsonResponse({"ok": False, "error": "end_at は start_at より後が必要"}, status=400)
    if _overlaps(channel, prog.start_at, end, prog.id):
        return JsonResponse({"ok": False, "error": "overlap"}, status=422)
    try:
        with transaction.atomic():
            prog.end_at = end
            prog.save(update_fields=["end_at"])
            _resolve_soon(channel.id)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "overlap"}, status=422)
    return JsonResponse({"ok": True, "end_at": end.isoformat()})


# ---- ad_break グラフィック編集 ----


def _grid_unit(grid: str) -> int:
    return 15000 if grid == CmGrid.G15 else 20000


def _breaks_total_ms(prog, extra_ms: int = 0) -> int:
    """録画番組の総尺 = 素材尺 + Σ(ad_break.duration) (+ 追加予定分)。end_at 算出用。"""
    base = prog.asset.duration_ms or 0
    existing = sum(b.duration_ms for b in prog.ad_breaks.all())
    return base + existing + extra_ms


def _validate_break(prog, offset_ms: int, grid: str, duration_ms: int) -> str | None:
    if grid not in CmGrid.values:
        return "grid が不正 (15s/20s)"
    unit = _grid_unit(grid)
    if duration_ms <= 0 or duration_ms % unit != 0:
        return f"duration_ms は {unit}ms の整数倍が必要"
    if prog.asset is None or prog.asset.duration_ms is None:
        return "録画素材が未設定"
    if offset_ms < 0 or offset_ms >= prog.asset.duration_ms:
        return "offset_ms が素材尺の範囲外"
    return None


@staff_member_required
@require_POST
def adbreak_create(request, slug: str, program_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(Program, pk=program_id, channel=channel)
    if prog.type != ProgramType.RECORDED:
        return JsonResponse({"ok": False, "error": "ad_break は録画番組のみ"}, status=422)
    data = _body(request)
    grid = data.get("grid", CmGrid.G15)
    offset_ms = _to_int(data.get("offset_ms"))
    duration_ms = _to_int(data.get("duration_ms", _grid_unit(grid)))
    if offset_ms is None or duration_ms is None:
        return JsonResponse({"ok": False, "error": "offset_ms/duration_ms が不正"}, status=400)
    err = _validate_break(prog, offset_ms, grid, duration_ms)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    # CM枠は尺を足す → end_at が伸びる (不変条件 end_at = start + 素材尺 + Σbreaks)。後続と重なるなら拒否
    new_end = prog.start_at + timedelta(milliseconds=_breaks_total_ms(prog, duration_ms))
    if _overlaps(channel, prog.start_at, new_end, prog.id):
        return JsonResponse(
            {"ok": False, "error": "CM枠を追加すると後続番組に重なります"}, status=422
        )
    with transaction.atomic():
        br = AdBreak.objects.create(
            program=prog, offset_ms=offset_ms, grid=grid, duration_ms=duration_ms
        )
        prog.end_at = new_end
        prog.save(update_fields=["end_at"])
        _resolve_soon(channel.id)
    return JsonResponse(
        {
            "ok": True,
            "id": br.id,
            "offset_ms": offset_ms,
            "grid": grid,
            "duration_ms": duration_ms,
            "end_at": new_end.isoformat(),
        }
    )


@staff_member_required
@require_POST
def adbreak_update(request, slug: str, adbreak_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    br = get_object_or_404(
        AdBreak.objects.select_related("program__asset"), pk=adbreak_id, program__channel=channel
    )
    offset_ms = _to_int(_body(request).get("offset_ms"))
    if offset_ms is None:
        return JsonResponse({"ok": False, "error": "offset_ms が不正"}, status=400)
    err = _validate_break(br.program, offset_ms, br.grid, br.duration_ms)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    br.offset_ms = offset_ms
    br.save(update_fields=["offset_ms"])
    _resolve_soon(channel.id)
    return JsonResponse({"ok": True, "id": br.id, "offset_ms": offset_ms})


@staff_member_required
@require_POST
def adbreak_delete(request, slug: str, adbreak_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    br = get_object_or_404(
        AdBreak.objects.select_related("program__asset"), pk=adbreak_id, program__channel=channel
    )
    prog = br.program
    cid = prog.channel_id
    with transaction.atomic():
        br.delete()
        # CM枠を抜いた分 end_at を縮める (短縮なので後続とは重ならない)
        if prog.type == ProgramType.RECORDED and prog.asset and prog.asset.duration_ms is not None:
            prog.end_at = prog.start_at + timedelta(milliseconds=_breaks_total_ms(prog))
            prog.save(update_fields=["end_at"])
        _resolve_soon(cid)
    return HttpResponse(status=204)


@staff_member_required
@require_POST
def adbreak_apply_cuesheet(request, slug: str, program_id: int):
    """素材のキューシートから Program.ad_break を再生成し end_at を再計算 (テンプレ適用)。

    既存 ad_break は置換する。適用後の end_at が後続番組に重なる場合は拒否 (前に番組を空ける運用)。
    """
    from medialib import services as ml
    from medialib.models import CueSheet

    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(
        Program.objects.select_related("asset"), pk=program_id, channel=channel
    )
    if prog.type != ProgramType.RECORDED or prog.asset is None:
        return JsonResponse({"ok": False, "error": "キューシート適用は録画番組のみ"}, status=422)
    cue = CueSheet.objects.filter(asset=prog.asset).first()
    if cue is None:
        return JsonResponse(
            {"ok": False, "error": "この素材にキューシートがありません"}, status=422
        )
    try:
        specs = ml.derive_ad_breaks(cue)
    except ml.CueSheetError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=422)

    cm_total = sum(s["duration_ms"] for s in specs)
    new_end = prog.start_at + timedelta(milliseconds=(prog.asset.duration_ms or 0) + cm_total)
    if _overlaps(channel, prog.start_at, new_end, prog.id):
        return JsonResponse(
            {"ok": False, "error": "適用すると後続番組に重なります (先に枠を空けてください)"},
            status=422,
        )
    with transaction.atomic():
        prog.ad_breaks.all().delete()
        AdBreak.objects.bulk_create([AdBreak(program=prog, **s) for s in specs])
        prog.end_at = new_end
        prog.save(update_fields=["end_at"])
        _resolve_soon(channel.id)
    return JsonResponse({"ok": True, "n_breaks": len(specs), "end_at": new_end.isoformat()})


# ---- 週間基本編成スロット (SeriesSlot) のグリッド D&D 編集 ----
# 週間グリッド (曜日×タイムライン) の下地レイヤ。実 Program と違い overlap 制約は無い
# (重なりは正当。展開時 expand_series_slots が EXCLUDE で解決)。検証は SeriesSlotForm を再利用。
# スロット編集は当週だけでなく全週へ波及 (投影) する点に注意 (UI 側でバナー明示)。


def _form_errors(form) -> str:
    return "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()) or "入力が不正です"


@staff_member_required
@require_POST
def slot_create_grid(request, slug: str):
    """グリッドの空きセルドラッグから SeriesSlot を新規作成。SeriesSlotForm で全検証。"""
    channel = get_object_or_404(Channel, slug=slug)
    data = _body(request)
    series = get_object_or_404(Series, pk=data.get("series_id"), channel=channel)
    # SeriesSlotForm は form-data 名 (default_asset/live_source/duration_min) を期待するので写す。
    form = SeriesSlotForm(
        {
            "dow": data.get("dow"),
            "start_time": data.get("start_time"),
            "duration_min": data.get("duration_min"),
            "program_type": data.get("program_type"),
            "default_asset": data.get("default_asset_id") or "",
            "live_source": data.get("live_source_id") or "",
            "recurrence_kind": data.get("recurrence_kind") or "",
            "weeks_csv": data.get("weeks_csv") or "",
            "days_csv": data.get("days_csv") or "",
            "ending_csv": data.get("ending_csv") or "",
            "effective_from": data.get("effective_from"),
            "effective_to": data.get("effective_to") or "",
        }
    )
    if not form.is_valid():
        return JsonResponse({"ok": False, "error": _form_errors(form)}, status=422)
    try:
        with transaction.atomic():
            slot = form.save(commit=False)
            slot.series = series
            slot.save()
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "スロットを保存できません"}, status=422)
    return JsonResponse(
        {"ok": True, "slot_id": slot.id, "recurrence_label": slot.recurrence_label()}
    )


@staff_member_required
@require_POST
def slot_move_grid(request, slug: str, slot_id: int):
    """スロットを別曜日/別時刻へドラッグ移動 (dow / start_time のみ変更)。"""
    slot = get_object_or_404(
        SeriesSlot.objects.select_related("series"), pk=slot_id, series__channel__slug=slug
    )
    data = _body(request)
    fields = []
    dow = _to_int(data.get("dow"))
    if dow is not None:
        if not (0 <= dow <= 6):
            return JsonResponse({"ok": False, "error": "dow は 0〜6"}, status=400)
        slot.dow = dow
        fields.append("dow")
    start_time = data.get("start_time")
    if start_time:
        parsed = dateparse.parse_time(start_time)
        if parsed is None:
            return JsonResponse({"ok": False, "error": "start_time が不正"}, status=400)
        slot.start_time = parsed
        fields.append("start_time")
    if not fields:
        return JsonResponse({"ok": False, "error": "変更がありません"}, status=400)
    try:
        with transaction.atomic():
            slot.save(update_fields=fields)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "スロットを移動できません"}, status=422)
    return JsonResponse(
        {"ok": True, "dow": slot.dow, "start_time": slot.start_time.strftime("%H:%M")}
    )


@staff_member_required
@require_POST
def slot_resize_grid(request, slug: str, slot_id: int):
    """スロットの尺をドラッグ変更 (duration_ms)。"""
    slot = get_object_or_404(SeriesSlot, pk=slot_id, series__channel__slug=slug)
    duration_ms = _to_int(_body(request).get("duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        return JsonResponse({"ok": False, "error": "duration_ms は正の整数"}, status=400)
    slot.duration_ms = duration_ms
    slot.save(update_fields=["duration_ms"])
    return JsonResponse({"ok": True, "duration_ms": duration_ms})


# ---- 生キューシート (LiveRundown/LiveCue) の編集 (タイムキープ Phase 1 / #25) ----
# kind↔FK 整合はDB制約でなくここで検証する (D5、AdBreak の _validate_break 踏襲)。
# state != pending の cue は編集・削除不可 (D6、発火/スキップ済みは as-run の記録)。


def _validate_cue(
    kind: str, cm_bundle_id: int | None, asset_id: int | None, grid: str | None
) -> str | None:
    if kind == LiveCueKind.CM:
        if grid not in CmGrid.values:
            return "grid が不正 (15s/20s)"
        # cm_bundle は任意: 無ければ発火時に grid で在庫から動的充填する (D3 §11)。
    elif kind == LiveCueKind.VT:
        if not asset_id:
            return "VT の cue には asset の指定が必要です"
    elif kind == LiveCueKind.SECTION:
        if cm_bundle_id or asset_id:
            return "本編の cue に cm_bundle/asset は指定できません"
    else:
        return "kind が不正です"
    return None


# 24時間 (常識的な生番組尺の上限)。resolver._emit_live_auto_fire_cues の
# prog.start_at + timedelta(milliseconds=...) が桁外れな値で OverflowError を起こし
# channel 丸ごとの resolve が止まる事故 (2026-07-04 レビューで実証) を、入力の境界で防ぐ。
_AUTO_OFFSET_MAX_MS = 24 * 3600 * 1000


def _validate_auto_offset(auto_offset_ms: int | None) -> str | None:
    if auto_offset_ms is None:
        return None
    if not (0 <= auto_offset_ms <= _AUTO_OFFSET_MAX_MS):
        return "auto_offset_ms は0以上24時間(ms)以内の整数が必要です"
    return None


def _parse_wall_time(s):
    """ "HH:MM"(:SS 可) → datetime.time | None。空/不正は None。"""
    return dateparse.parse_time(s) if s else None


def _apply_auto_fields(cue, data) -> str | None:
    """auto_fire/anchor 系の項目を検証して cue に反映する (live_cue/template 共有・D1 §11)。
    エラー文字列を返したら呼び出し側が 422 を返す (cue は保存しない)。"""
    auto_offset_ms = _to_int(data.get("auto_offset_ms"))
    auto_anchor = data.get("auto_anchor") or LiveCueAnchor.START
    if auto_anchor not in LiveCueAnchor.values:
        return "auto_anchor が不正です (start/wallclock)"
    auto_wall_time = _parse_wall_time(data.get("auto_wall_time"))
    if auto_anchor == LiveCueAnchor.WALLCLOCK:
        if auto_wall_time is None:
            return "壁時計アンカーには auto_wall_time (HH:MM) が必要です"
    else:  # start
        err = _validate_auto_offset(auto_offset_ms)
        if err:
            return err
    cue.auto_fire = bool(data.get("auto_fire"))
    cue.auto_offset_ms = auto_offset_ms
    cue.auto_anchor = auto_anchor
    cue.auto_wall_time = auto_wall_time
    return None


@staff_member_required
@require_POST
def live_cue_create(request, slug: str, program_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(Program, pk=program_id, channel=channel)
    if prog.type != ProgramType.LIVE:
        return JsonResponse({"ok": False, "error": "生キューシートは生番組のみ"}, status=422)
    data = _body(request)
    kind = data.get("kind", "")
    # 検証は body の生値で行う (section に紛れ込んだ cm_bundle_id/asset_id も弾く)。
    # 保存時のみ kind に応じた項目だけを残す。
    cm_bundle_id = _to_int(data.get("cm_bundle_id"))
    grid = data.get("grid") or None
    asset_id = _to_int(data.get("asset_id"))
    duration_ms = _to_int(data.get("planned_duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        return JsonResponse(
            {"ok": False, "error": "planned_duration_ms は正の整数が必要"}, status=400
        )
    err = _validate_cue(kind, cm_bundle_id, asset_id, grid)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    with transaction.atomic():
        rundown, _created = LiveRundown.objects.get_or_create(program=prog)
        next_seq = (rundown.cues.aggregate(m=Max("seq"))["m"] or 0) + 1
        cue = LiveCue.objects.create(
            rundown=rundown,
            seq=next_seq,
            kind=kind,
            label=(data.get("label") or "").strip() or None,
            planned_duration_ms=duration_ms,
            cm_bundle_id=cm_bundle_id if kind == LiveCueKind.CM else None,
            grid=grid if kind == LiveCueKind.CM else None,
            asset_id=asset_id if kind == LiveCueKind.VT else None,
        )
    return JsonResponse({"ok": True, "id": cue.id})


@staff_member_required
@require_POST
def live_cue_update(request, slug: str, cue_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    cue = get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    if cue.state != LiveCueState.PENDING:
        return JsonResponse(
            {"ok": False, "error": "発火済み/スキップ済みの cue は編集できません"}, status=422
        )
    data = _body(request)
    duration_ms = _to_int(data.get("planned_duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        return JsonResponse(
            {"ok": False, "error": "planned_duration_ms は正の整数が必要"}, status=400
        )
    # kind は不変 (POST body に来ても無視する)
    cm_bundle_id = _to_int(data.get("cm_bundle_id")) if cue.kind == LiveCueKind.CM else None
    grid = (data.get("grid") or None) if cue.kind == LiveCueKind.CM else None
    asset_id = _to_int(data.get("asset_id")) if cue.kind == LiveCueKind.VT else None
    err = _validate_cue(cue.kind, cm_bundle_id, asset_id, grid)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    cue.label = (data.get("label") or "").strip() or None
    cue.planned_duration_ms = duration_ms
    err = _apply_auto_fields(cue, data)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    if cue.kind == LiveCueKind.CM:
        cue.cm_bundle_id = cm_bundle_id
        cue.grid = grid
    elif cue.kind == LiveCueKind.VT:
        cue.asset_id = asset_id
    cue.save()
    return JsonResponse({"ok": True, "id": cue.id})


@staff_member_required
@require_POST
def live_cue_move(request, slug: str, cue_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    cue = get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    if cue.state != LiveCueState.PENDING:
        return JsonResponse(
            {"ok": False, "error": "発火済み/スキップ済みの cue は並べ替えできません"}, status=422
        )
    move_ordered_item(cue, cue.rundown.cues, _body(request).get("direction", ""))
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def live_cue_delete(request, slug: str, cue_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    cue = get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    if cue.state != LiveCueState.PENDING:
        return JsonResponse(
            {"ok": False, "error": "発火済み/スキップ済みの cue は削除できません"}, status=422
        )
    cue.delete()
    return HttpResponse(status=204)


# ---- 定番進行表 (LiveRundownTemplate/…Cue) の編集 (タイムキープ Phase3 C / #25 §9) ----
# SeriesSlot に紐づく「原本」。live_cue_* と同じ検証 (_validate_cue/_validate_auto_offset) を共有し、
# 展開時に LiveRundown/LiveCue へ複製される (scheduling.tasks.expand_series_slots)。state を持たない
# ので発火/スキップの遷移ガードは不要 (原本は常に編集可能)。


def _tmpl_cue_or_404(slug: str, cue_id: int) -> LiveRundownTemplateCue:
    channel = get_object_or_404(Channel, slug=slug)
    return get_object_or_404(
        LiveRundownTemplateCue.objects.select_related("template__slot__series__channel"),
        pk=cue_id,
        template__slot__series__channel=channel,
    )


@staff_member_required
@require_POST
def template_cue_create(request, slug: str, slot_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    slot = get_object_or_404(
        SeriesSlot.objects.select_related("series"), pk=slot_id, series__channel=channel
    )
    if slot.program_type != ProgramType.LIVE:
        return JsonResponse({"ok": False, "error": "定番進行表は生スロットのみ"}, status=422)
    data = _body(request)
    kind = data.get("kind", "")
    cm_bundle_id = _to_int(data.get("cm_bundle_id"))
    grid = data.get("grid") or None
    asset_id = _to_int(data.get("asset_id"))
    duration_ms = _to_int(data.get("planned_duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        return JsonResponse(
            {"ok": False, "error": "planned_duration_ms は正の整数が必要"}, status=400
        )
    err = _validate_cue(kind, cm_bundle_id, asset_id, grid)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    with transaction.atomic():
        template, _created = LiveRundownTemplate.objects.get_or_create(slot=slot)
        next_seq = (template.cues.aggregate(m=Max("seq"))["m"] or 0) + 1
        cue = LiveRundownTemplateCue.objects.create(
            template=template,
            seq=next_seq,
            kind=kind,
            label=(data.get("label") or "").strip() or None,
            planned_duration_ms=duration_ms,
            cm_bundle_id=cm_bundle_id if kind == LiveCueKind.CM else None,
            grid=grid if kind == LiveCueKind.CM else None,
            asset_id=asset_id if kind == LiveCueKind.VT else None,
        )
    return JsonResponse({"ok": True, "id": cue.id})


@staff_member_required
@require_POST
def template_cue_update(request, slug: str, cue_id: int):
    cue = _tmpl_cue_or_404(slug, cue_id)
    data = _body(request)
    duration_ms = _to_int(data.get("planned_duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        return JsonResponse(
            {"ok": False, "error": "planned_duration_ms は正の整数が必要"}, status=400
        )
    # kind は不変 (live_cue_update と同じ流儀)。
    cm_bundle_id = _to_int(data.get("cm_bundle_id")) if cue.kind == LiveCueKind.CM else None
    grid = (data.get("grid") or None) if cue.kind == LiveCueKind.CM else None
    asset_id = _to_int(data.get("asset_id")) if cue.kind == LiveCueKind.VT else None
    err = _validate_cue(cue.kind, cm_bundle_id, asset_id, grid)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    cue.label = (data.get("label") or "").strip() or None
    cue.planned_duration_ms = duration_ms
    err = _apply_auto_fields(cue, data)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=422)
    if cue.kind == LiveCueKind.CM:
        cue.cm_bundle_id = cm_bundle_id
        cue.grid = grid
    elif cue.kind == LiveCueKind.VT:
        cue.asset_id = asset_id
    cue.save()
    return JsonResponse({"ok": True, "id": cue.id})


@staff_member_required
@require_POST
def template_cue_move(request, slug: str, cue_id: int):
    cue = _tmpl_cue_or_404(slug, cue_id)
    move_ordered_item(cue, cue.template.cues, _body(request).get("direction", ""))
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def template_cue_delete(request, slug: str, cue_id: int):
    cue = _tmpl_cue_or_404(slug, cue_id)
    cue.delete()
    return HttpResponse(status=204)
