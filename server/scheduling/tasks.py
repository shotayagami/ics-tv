# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scheduling の非同期タスク。リゾルバ (編成 -> playout_event)。詳細は docs/scheduler.md。

Celery beat が定期的に resolve_all_channels を起動。各 channel ごとに resolve_window が走り、
[now, now+48h) の編成を冪等 upsert する。idempotency_key は決定論的に算出されるため、
再解決は自然な diff になり、agent には sync_seq の増分として伝搬される。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Channel
from scheduling.models import (
    AdBreak,
    Episode,
    GraphicCue,
    LiveCue,
    LiveRundown,
    LiveRundownTemplate,
    Program,
    ProgramType,
    SeriesSlot,
)
from scheduling.resolver import resolve

logger = logging.getLogger(__name__)


@shared_task
def ingest_live_recordings(scan_limit: int = 200) -> dict[str, int]:
    """生放送録画取り込み: R2 ドロップを scan → Asset 化、正規化 READY を Program.recording_asset へ bind。"""
    from scheduling import live_recording

    created = live_recording.scan(limit=scan_limit)
    stats = live_recording.bind()
    result = {"scanned_new": created, **stats}
    logger.info("ingest_live_recordings %s", result)
    return result


@shared_task
def resolve_window(channel_id: int, hours: int = 48) -> dict[str, int]:
    """1 ch の [now, now+hours) を解決して playout_event を冪等 upsert。"""
    channel = Channel.objects.get(pk=channel_id, enabled=True)
    t0 = timezone.now()
    t1 = t0 + timedelta(hours=hours)
    stats = resolve(channel, t0, t1)
    logger.info(
        "resolve_window channel=%s window=[%s,%s) stats=%s",
        channel.slug,
        t0.isoformat(),
        t1.isoformat(),
        stats,
    )
    return stats


@shared_task
def resolve_all_channels(hours: int = 48) -> dict[int, dict[str, int]]:
    """enabled な全 channel を順に resolve。Celery beat から定期呼び出し。"""
    results: dict[int, dict[str, int]] = {}
    for channel in Channel.objects.filter(enabled=True):
        try:
            results[channel.id] = resolve_window(channel.id, hours)
        except Exception:
            logger.exception("resolve_window failed channel=%s", channel.slug)
            results[channel.id] = {"error": 1}
    return results


@shared_task
def resolve_channel_now(channel_id: int) -> dict[str, int]:
    """押え/巻き/編成変更からの単一 ch 即時再解決 (docs/operations.md O-C)。

    5 分周期の resolve_all_channels を待たず当該 ch のみ即時に再解決する。
    extend/shorten が transaction.on_commit から .delay する (commit 後の確定編成で解決)。
    """
    return resolve_window(channel_id)


# ---- 週間基本編成の展開 (#6 Phase C) ----


def _slot_matches(slot: SeriesSlot, d) -> bool:
    """日付 d が slot の繰り返しパターンに該当するか (effective 窓は呼び出し側で判定)。"""
    return slot.matches(d)


def _slot_dates(slot: SeriesSlot, start_date, horizon):
    """slot の繰り返しパターンに該当し、effective 窓 ∩ [start_date, horizon) に入る日付を列挙。"""
    d = start_date
    while d < horizon:
        if (
            d >= slot.effective_from
            and (slot.effective_to is None or d <= slot.effective_to)
            and _slot_matches(slot, d)
        ):
            yield d
        d += timedelta(days=1)


def _asset_cuesheet_plan(asset_id: int | None, cuesheets: dict):
    """asset にキューシートがあれば (ad_break spec 列, 総尺ms) を返す。

    無効なキューシート (検証 NG) や未設定なら (None, None) → 呼び出し側は slot.duration_ms で展開。
    """
    from medialib import services as ml

    cue = cuesheets.get(asset_id) if asset_id else None
    if cue is None:
        return None, None
    try:
        specs = ml.derive_ad_breaks(cue)
    except ml.CueSheetError:
        return None, None  # 不正なキューシートは無視 (slot.duration_ms で展開)
    return specs, ml.cue_total_airtime_ms(cue)


def _slot_cuesheet_plan(slot: SeriesSlot, cuesheets: dict):
    """slot の既定素材の「基本キューシート」プラン (#7 Phase 2)。回別素材で上書きされる場合は
    _asset_cuesheet_plan を直接使う。"""
    return _asset_cuesheet_plan(slot.default_asset_id, cuesheets)


@shared_task
def expand_series_slots(weeks: int = 4) -> dict:
    """有効な series_slot を N 週先まで Program へ展開する (#6 Phase C)。

    bulk insert は 1 件の EXCLUDE 違反で全体が失敗するため不可。行単位 savepoint
    (transaction.atomic ネスト) で展開し、衝突行 (手動編成優先 or 既展開) のみスキップ + 計上する。
    既定素材にキューシートがあれば ad_break を自動展開し end_at を総尺で算出 (#7 Phase 2)。
    承認済み納品で確定した回別素材 (Episode.asset) があれば default_asset より優先採用する (#5)。
    """
    from medialib.models import CueSheet, NormalizeStatus

    today = timezone.now().date()
    horizon = today + timedelta(weeks=weeks)
    created = skipped = 0
    slots = list(
        SeriesSlot.objects.filter(
            series__is_active=True, effective_from__lte=horizon
        ).select_related("series")
    )
    # 回別に素材確定した Episode を (series, air_date) で索引化 (READY のみ＝送出可能な素材に限る)。
    # get_or_create はしない: 納品が先に作った回だけリンクする (#5 設計要点3)。
    episode_by_key = {
        (ep.series_id, ep.air_date): ep
        for ep in Episode.objects.filter(
            series_id__in={s.series_id for s in slots},
            air_date__gte=today,
            air_date__lt=horizon,
            asset__isnull=False,
            asset__normalize_status=NormalizeStatus.READY,
        ).select_related("asset")
    }
    # 既定素材 + 回別素材のキューシートをまとめて取得 (N+1 回避)
    asset_ids = {s.default_asset_id for s in slots if s.default_asset_id}
    asset_ids |= {ep.asset_id for ep in episode_by_key.values() if ep.asset_id}
    cuesheets = {
        cs.asset_id: cs
        for cs in CueSheet.objects.filter(asset_id__in=asset_ids).prefetch_related("points")
    }
    # 生スロットの定番進行表(雛形)を slot_id で索引化 (#25 Phase3 C・N+1 回避)。live 分岐が
    # 展開する Program ごとに LiveRundown/LiveCue へ複製する。
    templates_by_slot = {
        t.slot_id: t
        for t in LiveRundownTemplate.objects.filter(
            slot_id__in={s.id for s in slots if s.program_type == ProgramType.LIVE}
        ).prefetch_related("cues")
    }
    for slot in slots:
        slot_specs, slot_airtime = _slot_cuesheet_plan(slot, cuesheets)
        for d in _slot_dates(slot, today, horizon):
            # 回別素材があれば優先 (recorded のみ。live は asset を持たない=chk_program_source)。
            ep = (
                episode_by_key.get((slot.series_id, d))
                if slot.program_type == ProgramType.RECORDED
                else None
            )
            asset_id: int | None
            if ep is not None and ep.asset_id:
                specs, airtime_ms = _asset_cuesheet_plan(ep.asset_id, cuesheets)
                asset_id = ep.asset_id
            else:
                specs, airtime_ms = slot_specs, slot_airtime
                asset_id = slot.default_asset_id
            dur_ms = airtime_ms if airtime_ms is not None else slot.duration_ms
            start = timezone.make_aware(datetime.combine(d, slot.start_time))
            end = start + timedelta(milliseconds=dur_ms)
            try:
                with transaction.atomic():
                    prog = Program.objects.create(
                        channel_id=slot.series.channel_id,
                        series=slot.series,
                        episode=ep,
                        type=slot.program_type,
                        title=slot.series.title,
                        genre=slot.series.genre,  # #7 公開フロント: シリーズのジャンルを展開 Program へ継承
                        start_at=start,
                        end_at=end,
                        asset_id=asset_id,
                        live_source_id=slot.live_source_id,
                    )
                    if specs:
                        AdBreak.objects.bulk_create([AdBreak(program=prog, **s) for s in specs])
                    # Series の自動グラフィックセット原本を個別回 Program へコピー (#18 §C)。
                    series_cues = list(slot.series.graphic_cues.all())
                    if series_cues:
                        GraphicCue.objects.bulk_create(
                            [
                                GraphicCue(
                                    program=prog,
                                    layer=c.layer,
                                    kind=c.kind,
                                    data=c.data,
                                    template=c.template,
                                    show_at_ms=c.show_at_ms,
                                    hide_at_ms=c.hide_at_ms,
                                    seq=c.seq,
                                )
                                for c in series_cues
                            ]
                        )
                    # 生番組は定番進行表(雛形)を LiveRundown/LiveCue へ複製 (#25 Phase3 C §9)。
                    # state は既定 pending、fired_event は null (原本は発火情報を持たない)。
                    if slot.program_type == ProgramType.LIVE:
                        template = templates_by_slot.get(slot.id)
                        tmpl_cues = list(template.cues.all()) if template else []
                        if tmpl_cues:
                            rundown = LiveRundown.objects.create(program=prog)
                            LiveCue.objects.bulk_create(
                                [
                                    LiveCue(
                                        rundown=rundown,
                                        seq=tc.seq,
                                        kind=tc.kind,
                                        label=tc.label,
                                        planned_duration_ms=tc.planned_duration_ms,
                                        cm_bundle_id=tc.cm_bundle_id,
                                        grid=tc.grid,
                                        asset_id=tc.asset_id,
                                        auto_fire=tc.auto_fire,
                                        auto_offset_ms=tc.auto_offset_ms,
                                        auto_anchor=tc.auto_anchor,
                                        auto_wall_time=tc.auto_wall_time,
                                    )
                                    for tc in tmpl_cues
                                ]
                            )
                created += 1
            except IntegrityError:
                skipped += 1  # EXCLUDE 衝突 (手動編成優先 / 既展開)
    logger.info("expand_series_slots created=%d skipped=%d", created, skipped)
    return {"created": created, "skipped": skipped}


@shared_task
def apply_episode_asset(asset_id: int) -> dict:
    """正規化(READY)された回別素材を、既に展開済みの未来 Program へ反映する (#5)。

    承認時に Episode.asset を確定する時点では素材は PENDING (尺なし) のため Program を差し替えられない。
    正規化完了で READY になったあと本タスクが、当該 Episode を指す/該当回 (series, air_date) の
    未来 (start_at > now)・RECORDED な Program の asset/end_at/ad_break を作り直す。EXCLUDE 衝突
    (新尺が後続に食い込む) は per-Program savepoint で捕捉し、差替を見送って通知する (握り潰さない)。
    通常運用 (納品→正規化が展開より先) ではこの経路は不要で、expand_series_slots が直接採用する。
    """
    from datetime import time

    from core.notify import notify
    from medialib.models import Asset, CueSheet, NormalizeStatus

    asset = Asset.objects.filter(pk=asset_id, normalize_status=NormalizeStatus.READY).first()
    if asset is None or asset.duration_ms is None:
        return {"swapped": 0, "skipped": 0, "reason": "asset_not_ready"}

    cue = CueSheet.objects.filter(asset_id=asset_id).prefetch_related("points").first()
    specs, airtime_ms = _asset_cuesheet_plan(asset_id, {asset_id: cue} if cue else {})
    dur_ms = airtime_ms if airtime_ms is not None else asset.duration_ms

    now = timezone.now()
    tz = timezone.get_current_timezone()
    channels: set[int] = set()
    swapped = skipped = 0
    for ep in Episode.objects.filter(asset_id=asset_id).select_related("series"):
        # 対象 = この回にリンク済み Program、または該当回 (series, air_date) で既定素材のまま展開済みの
        # 未リンク Program。既にこの素材を持つものは除外 (冪等)。
        cond = Q(episode_id=ep.id)
        if ep.air_date:
            day_start = timezone.make_aware(datetime.combine(ep.air_date, time.min), tz)
            cond |= Q(
                series_id=ep.series_id,
                episode__isnull=True,
                start_at__gte=day_start,
                start_at__lt=day_start + timedelta(days=1),
            )
        progs = Program.objects.filter(cond, type=ProgramType.RECORDED, start_at__gt=now).exclude(
            asset_id=asset_id
        )
        for prog in progs:
            new_end = prog.start_at + timedelta(milliseconds=dur_ms)
            try:
                with transaction.atomic():
                    prog.ad_breaks.all().delete()  # 既存 CM枠を作り直す (再解決で再充填)
                    prog.asset_id = asset_id
                    prog.episode = ep
                    prog.end_at = new_end
                    prog.save(update_fields=["asset", "episode", "end_at"])
                    if specs:
                        AdBreak.objects.bulk_create([AdBreak(program=prog, **s) for s in specs])
                swapped += 1
                channels.add(prog.channel_id)
            except IntegrityError:
                # 新尺が後続番組に食い込む (EXCLUDE)。差替せず編成調整を促す。
                skipped += 1
                notify(
                    "warn",
                    "episode_swap_conflict",
                    f"回別素材の差替が後続番組と衝突 (program={prog.id} episode={ep.id})。"
                    "尺が枠を超過しています。編成を調整してください。",
                    channel=prog.channel_id,
                )
    for c in channels:
        resolve_channel_now.delay(c)  # 差替後の編成を即時再解決 → playout_event に伝搬
    logger.info("apply_episode_asset asset=%s swapped=%d skipped=%d", asset_id, swapped, skipped)
    return {"swapped": swapped, "skipped": skipped}
