# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""編成タイムライン (docs/ui.md 「編成タイムライン」)。Phase 1 最小: 一覧 + 新規作成。

Phase 1 後半でドラッグ移動・端リサイズ・ad_break グラフィック編集を追加 (Alpine.js)。
"""

from __future__ import annotations

import re
from datetime import timedelta

from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core import thumbnails
from core.forms import BootstrapFormMixin
from core.models import Channel
from medialib.models import CmCreative, NormalizeStatus
from medialib.services import program_airtime_ms
from scheduling.edit_views import _resolve_soon
from scheduling.forms import DOW_CHOICES, SeriesForm, SeriesSlotForm
from scheduling.models import AdBreak, AdBreakItem, Program, ProgramType, Series, SeriesSlot
from scheduling.tasks import expand_series_slots


class ProgramForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Program
        fields = [
            "title",
            "type",
            "genre",
            "exposure_policy",
            "start_at",
            "end_at",
            "asset",
            "live_source",
            "cm_bundle",
            "description",
            "cast",
            "public_visible",
            "vod_visibility",
            "vod_available_until",
            "clock_hidden",
            "lbar_hidden",
            # #23 YouTube 番組専用枠。空の preset は series.youtube_preset にフォールバック。
            "youtube_dedicated",
            "youtube_preset",
            # 生放送の自動録画→見逃し配信化。type=live のときのみ意味を持つ opt-in (UI もこのとき限定表示)。
            "record_live",
        ]
        widgets = {
            # step="1" = 秒精度。既定(分刻み)だと既存値の秒(例 :07)がグリッド外で弾かれ、
            # :00 も入力できなくなる。放送はフレーム/秒精度が要るので秒まで許可する。
            # format= は値描画用。既定の DATETIME_INPUT_FORMATS[0] はスペース区切り
            # ("2026-06-14 15:07:00") で datetime-local に載らず、編集時に既存値が
            # 空欄表示になる。T 区切りに固定して保存済み時刻を正しく描画する
            # (送信は DateTimeField.to_python が parse_datetime で T を受理)。
            "start_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
            ),
            "end_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
            ),
            "vod_available_until": forms.DateTimeInput(
                attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
            ),
            "description": forms.Textarea(attrs={"rows": 3}),
            "cast": forms.Textarea(attrs={"rows": 2, "placeholder": "出演者 (カンマ/改行区切り)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # end_at は asset 尺から自動算出するため任意入力に (生放送は手入力)。
        end_field = self.fields["end_at"]
        end_field.required = False
        end_field.help_text = (
            "空欄なら素材尺 (キューシートがあれば CM枠込みの積算尺) から自動算出。"
        )
        # #23 YouTube 専用枠 (rolling 枠と並行)。preset 未指定なら series.youtube_preset を使う。
        self.fields["youtube_preset"].required = False
        self.fields["youtube_preset"].help_text = "空欄ならシリーズの既定プリセット"

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_at")
        end = cleaned.get("end_at")
        asset = cleaned.get("asset")
        # 新規作成時は素材尺から end_at を自動算出 (上書き)。
        # 編集時は手動調整 (ad_break リサイズ等) を尊重し、空欄のときだけ補完する。
        creating = self.instance.pk is None
        if start and asset is not None and (creating or not end):
            airtime = program_airtime_ms(asset)
            if airtime and airtime > 0:
                cleaned["end_at"] = start + timedelta(milliseconds=airtime)
                end = cleaned["end_at"]
            elif not end:
                self.add_error(
                    "end_at",
                    "素材尺が未取得のため終了時刻を自動算出できません"
                    " (正規化後に再試行するか手動入力してください)。",
                )
        elif not end:
            self.add_error(
                "end_at",
                "終了時刻を入力してください (生放送など素材の無い番組は自動算出できません)。",
            )
        if start and end and end <= start:
            self.add_error("end_at", "終了時刻は開始時刻より後にしてください。")
        # record_live は type=live のときのみ意味を持つ opt-in。録画番組では常に False に強制する
        # (UI では type=live のときのみ表示されるが、フォーム直POST等でのすり抜けを防ぐ)。
        if cleaned.get("type") != ProgramType.LIVE:
            cleaned["record_live"] = False
        return cleaned


def _channels_ctx(slug: str | None) -> dict:
    channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    return {"channels": channels, "current_ch_slug": slug}


# タイムラインの範囲選択 (空き領域ドラッグ) から ?start=&end= で渡る datetime-local 文字列。
# 文字列のまま初期値にすると widget がそのまま value 描画する (datetime を渡すと
# DATETIME_INPUT_FORMATS がスペース区切りで datetime-local に載らないため文字列で渡す)。
_DTLOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _timeline_prefill(request) -> dict:
    initial: dict[str, str] = {}
    for qs_key, field in (("start", "start_at"), ("end", "end_at")):
        v = request.GET.get(qs_key, "")
        if _DTLOCAL_RE.match(v):
            initial[field] = v
    return initial


def _asset_airtime_map() -> dict[int, int]:
    """{asset_id: 番組配置時の総尺(ms)} を 2 クエリで構築 (番組追加フォームの end_at 自動算出 JS 用)。

    キューシートがあれば CM枠込みの積算尺、なければ素材尺。尺未取得の素材は含めない。
    """
    from django.db.models import Sum

    from medialib.models import Asset, CuePoint

    cue_totals = dict(
        CuePoint.objects.values("cue_sheet__asset")
        .annotate(total=Sum("duration_ms"))
        .values_list("cue_sheet__asset", "total")
    )
    out: dict[int, int] = {}
    for aid, dur in Asset.objects.values_list("id", "duration_ms"):
        airtime = cue_totals.get(aid, dur)
        if airtime:
            out[aid] = airtime
    return out


@staff_member_required
def timeline(request, slug: str):
    channel = get_object_or_404(Channel, slug=slug)
    request.session["current_ch"] = slug
    now = timezone.now()
    horizon = now + timedelta(hours=24)
    programs = list(
        Program.objects.filter(
            channel=channel,
            end_at__gt=now - timedelta(hours=1),
            start_at__lt=horizon,
        )
        .select_related("asset", "live_source", "series")
        .prefetch_related("ad_breaks")
        .order_by("start_at")
    )
    # キューシートを持つ素材 (適用ボタンの表示判定に使う)
    from medialib.models import CueSheet

    cue_asset_ids = set(
        CueSheet.objects.filter(
            asset_id__in=[p.asset_id for p in programs if p.asset_id]
        ).values_list("asset_id", flat=True)
    )
    # グラフィカル描画 + ドラッグ編集用の JSON (json_script 経由でテンプレに渡す)
    programs_data = [
        {
            "id": p.id,
            "type": p.type,
            "title": p.title,
            "start_at": p.start_at.isoformat(),
            "end_at": p.end_at.isoformat(),
            "public_visible": p.public_visible,
            "source": (p.asset.title if p.asset else (p.live_source.name if p.live_source else "")),
            "thumb": p.thumb_url,
            "asset_duration_ms": (p.asset.duration_ms if p.asset else None),
            "has_cuesheet": p.asset_id in cue_asset_ids,
            "breaks": [
                {"id": b.id, "offset_ms": b.offset_ms, "grid": b.grid, "duration_ms": b.duration_ms}
                for b in sorted(p.ad_breaks.all(), key=lambda b: b.offset_ms)
            ],
        }
        for p in programs
    ]
    return render(
        request,
        "scheduling/timeline.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "programs": programs,
            "programs_data": programs_data,
            "now": now,
            "horizon": horizon,
            "active": "timeline",
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def program_create(request, slug: str):
    channel = get_object_or_404(Channel, slug=slug)
    if request.method == "POST":
        form = ProgramForm(request.POST)
        if form.is_valid():
            program = form.save(commit=False)
            program.channel = channel
            program.save()
            return redirect("scheduling:timeline", slug=slug)
    else:
        form = ProgramForm(initial=_timeline_prefill(request))
    return render(
        request,
        "scheduling/program_form.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "form": form,
            "active": "timeline",
            "is_edit": False,
            "asset_airtime_map": _asset_airtime_map(),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def program_update(request, slug: str, program_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    program = get_object_or_404(Program, pk=program_id, channel=channel)
    if request.method == "POST":
        form = ProgramForm(request.POST, instance=program)
        if form.is_valid():
            form.save()
            return redirect("scheduling:timeline", slug=slug)
    else:
        form = ProgramForm(instance=program)
    return render(
        request,
        "scheduling/program_form.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "form": form,
            "program": program,
            "active": "timeline",
            "is_edit": True,
        },
    )


@staff_member_required
@require_POST
def program_delete(request, slug: str, program_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    program = get_object_or_404(Program, pk=program_id, channel=channel)
    program.delete()
    return redirect("scheduling:timeline", slug=slug)


# ---- CM枠への CM 手動割当 (#7。fill_break は既存 AdBreakItem を尊重=手動割当が送出される) ----


def _fmt_ms(ms: int | None) -> str:
    s = (ms or 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


@staff_member_required
@require_http_methods(["GET"])
def program_breaks(request, slug: str, program_id: int):
    """録画番組の各 CM枠に CM を手動割当する画面。枠の定義自体はキューシート→キュー適用。"""
    channel = get_object_or_404(Channel, slug=slug)
    prog = get_object_or_404(
        Program.objects.select_related("asset"), pk=program_id, channel=channel
    )
    # grid 別の割当候補 CM (READY)。各枠 row にその grid のプールを添える。
    cms = {
        g: list(
            CmCreative.objects.select_related("asset")
            .filter(grid=g, asset__normalize_status=NormalizeStatus.READY)
            .order_by("advertiser")
        )
        for g in ("15s", "20s")
    }
    rows = []
    for br in prog.ad_breaks.order_by("offset_ms"):
        unit = 15000 if br.grid == "15s" else 20000
        items = list(br.items.select_related("cm_asset__asset").order_by("seq"))
        used = sum((it.cm_asset.asset.duration_ms or 0) for it in items)
        rows.append(
            {
                "br": br,
                "items": items,
                "slots": br.duration_ms // unit if unit else 0,
                "remaining_ms": br.duration_ms - used,
                "offset_disp": _fmt_ms(br.offset_ms),
                "dur_s": br.duration_ms // 1000,
                "cms": cms.get(br.grid, []),
            }
        )
    return render(
        request,
        "scheduling/breaks.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "program": prog,
            "rows": rows,
            "active": "timeline",
        },
    )


@staff_member_required
@require_POST
def adbreak_item_add(request, adbreak_id: int):
    br = get_object_or_404(AdBreak.objects.select_related("program__channel"), pk=adbreak_id)
    cm = get_object_or_404(CmCreative, pk=request.POST.get("cm_id"))
    if cm.grid != br.grid:
        return redirect(
            "scheduling:program_breaks", slug=br.program.channel.slug, program_id=br.program_id
        )
    with transaction.atomic():
        next_seq = (br.items.aggregate(m=Max("seq"))["m"] or -1) + 1
        AdBreakItem.objects.create(ad_break=br, seq=next_seq, cm_asset=cm)
        _resolve_soon(br.program.channel_id)
    return redirect(
        "scheduling:program_breaks", slug=br.program.channel.slug, program_id=br.program_id
    )


@staff_member_required
@require_POST
def adbreak_item_delete(request, item_id: int):
    it = get_object_or_404(
        AdBreakItem.objects.select_related("ad_break__program__channel"), pk=item_id
    )
    prog = it.ad_break.program
    it.delete()
    _resolve_soon(prog.channel_id)
    return redirect("scheduling:program_breaks", slug=prog.channel.slug, program_id=prog.id)


# ---- 週間基本編成 (Series / SeriesSlot) UI (#6 Phase C / Phase 2) ----

_DOW_LABELS = dict(DOW_CHOICES)


def _series_edit_ctx(slug, channel, series, form, slot_form):
    from medialib.models import CueSheet

    slots = list(
        series.slots.select_related("default_asset", "live_source").order_by("dow", "start_time")
    )
    # 既定素材にキューシートがあるスロットは展開時に CM枠 を自動付与する (#7 Phase 2)
    cue_asset_ids = set(
        CueSheet.objects.filter(
            asset_id__in={sl.default_asset_id for sl in slots if sl.default_asset_id}
        ).values_list("asset_id", flat=True)
    )
    rows = [
        {
            "slot": sl,
            "dow": _DOW_LABELS.get(sl.dow, sl.dow),
            "dur_min": sl.duration_ms // 60000,
            "has_cue": sl.default_asset_id in cue_asset_ids,
        }
        for sl in slots
    ]
    return {
        **_channels_ctx(slug),
        "channel": channel,
        "series": series,
        "form": form,
        "slot_form": slot_form,
        "rows": rows,
        "active": "series",
    }


@staff_member_required
@require_http_methods(["GET", "POST"])
def series_list(request, slug: str):
    channel = get_object_or_404(Channel, slug=slug)
    if request.method == "POST":
        form = SeriesForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False)
            s.channel = channel
            try:
                up = thumbnails.apply_upload(request)
            except thumbnails.ThumbnailError as e:
                form.add_error(None, str(e))
            else:
                if up:
                    s.thumbnail_url = up
                s.save()
                return redirect("scheduling:series_edit", slug=slug, series_id=s.id)
    else:
        form = SeriesForm()
    rows = [
        {"s": s, "n_slots": s.slots.count()}
        for s in Series.objects.filter(channel=channel)
        .prefetch_related("slots")
        .order_by("-is_active", "title")
    ]
    return render(
        request,
        "scheduling/series_list.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "form": form,
            "rows": rows,
            "active": "series",
            "expanded": request.GET.get("expanded"),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def series_edit(request, slug: str, series_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    s = get_object_or_404(Series, pk=series_id, channel=channel)
    if request.method == "POST":
        form = SeriesForm(request.POST, instance=s)
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
                return redirect("scheduling:series_edit", slug=slug, series_id=s.id)
    else:
        form = SeriesForm(instance=s)
    return render(
        request,
        "scheduling/series_edit.html",
        _series_edit_ctx(slug, channel, s, form, SeriesSlotForm()),
    )


@staff_member_required
@require_POST
def series_delete(request, slug: str, series_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    get_object_or_404(Series, pk=series_id, channel=channel).delete()
    return redirect("scheduling:series_list", slug=slug)


@staff_member_required
@require_POST
def slot_add(request, slug: str, series_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    s = get_object_or_404(Series, pk=series_id, channel=channel)
    form = SeriesSlotForm(request.POST)
    if form.is_valid():
        slot = form.save(commit=False)
        slot.series = s
        slot.save()
        return redirect("scheduling:series_edit", slug=slug, series_id=s.id)
    # バリデーション失敗 → エラー付きで再描画
    return render(
        request,
        "scheduling/series_edit.html",
        _series_edit_ctx(slug, channel, s, SeriesForm(instance=s), form),
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def slot_edit(request, slug: str, slot_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    slot = get_object_or_404(
        SeriesSlot.objects.select_related("series"), pk=slot_id, series__channel=channel
    )
    if request.method == "POST":
        form = SeriesSlotForm(request.POST, instance=slot)
        if form.is_valid():
            form.save()
            return redirect("scheduling:series_edit", slug=slug, series_id=slot.series_id)
    else:
        form = SeriesSlotForm(instance=slot)
    return render(
        request,
        "scheduling/slot_form.html",
        {
            **_channels_ctx(slug),
            "channel": channel,
            "series": slot.series,
            "form": form,
            "active": "series",
        },
    )


@staff_member_required
@require_POST
def slot_delete(request, slug: str, slot_id: int):
    channel = get_object_or_404(Channel, slug=slug)
    slot = get_object_or_404(
        SeriesSlot.objects.select_related("series"), pk=slot_id, series__channel=channel
    )
    series_id = slot.series_id
    slot.delete()
    return redirect("scheduling:series_edit", slug=slug, series_id=series_id)


@staff_member_required
@require_POST
def series_expand_now(request, slug: str):
    """有効な週間スロットを 4 週先まで Program へ手動展開 (beat も週次で実行)。冪等。"""
    from django.urls import reverse

    res = expand_series_slots(weeks=4)
    url = reverse("scheduling:series_list", kwargs={"slug": slug})
    return redirect(f"{url}?expanded={res['created']}-{res['skipped']}")
