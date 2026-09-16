# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase C C1: series_slot モデル + expand_series_slots (週次展開・行単位 savepoint)。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from scheduling.models import (
    Episode,
    EpisodeStatus,
    Program,
    ProgramType,
    RecurrenceKind,
    Series,
    SeriesSlot,
)
from scheduling.tasks import _slot_matches, expand_series_slots


def _series(channel):
    return Series.objects.create(channel=channel, title="毎日ニュース")


def _slot(series, asset, dow, hhmm="19:00", dur_ms=1_800_000, eff_from=None, eff_to=None):
    return SeriesSlot.objects.create(
        series=series,
        dow=dow,
        start_time=hhmm,
        duration_ms=dur_ms,
        program_type=ProgramType.RECORDED,
        default_asset=asset,
        effective_from=eff_from or date(2020, 1, 1),
        effective_to=eff_to,
    )


def test_slot_source_check_recorded_needs_asset(channel):
    s = _series(channel)
    # recorded なのに default_asset 無し → chk_slot_source 違反
    with pytest.raises(IntegrityError), transaction.atomic():
        SeriesSlot.objects.create(
            series=s,
            dow=0,
            start_time="19:00",
            duration_ms=1_800_000,
            program_type=ProgramType.RECORDED,
            effective_from=date(2026, 1, 1),
        )


def test_expand_creates_programs_for_next_weeks(channel, asset_ready):
    s = _series(channel)
    # 毎週 月曜 と 木曜
    _slot(s, asset_ready, dow=0)
    _slot(s, asset_ready, dow=3)
    res = expand_series_slots(weeks=2)
    # 2 週ぶん × 2 曜日 = 最大 4 (今日以降の該当日数に依存するが 3〜4)
    assert res["created"] >= 3
    progs = Program.objects.filter(series=s)
    assert progs.count() == res["created"]
    # 展開された Program は series 紐付け + 録画(asset)
    p = progs.first()
    assert p.series_id == s.id and p.asset_id == asset_ready.id


def test_expand_is_idempotent_skips_existing(channel, asset_ready):
    s = _series(channel)
    _slot(s, asset_ready, dow=0)
    first = expand_series_slots(weeks=3)
    assert first["created"] >= 1
    second = expand_series_slots(weeks=3)  # 再実行
    assert second["created"] == 0  # EXCLUDE 衝突で全スキップ
    assert second["skipped"] == first["created"]


def test_expand_respects_effective_window(channel, asset_ready):
    s = _series(channel)
    past = timezone.now().date() - timedelta(days=30)
    # 既に終了した slot は展開されない
    _slot(s, asset_ready, dow=0, eff_from=past - timedelta(days=30), eff_to=past)
    res = expand_series_slots(weeks=4)
    assert res["created"] == 0


def test_expand_skips_inactive_series(channel, asset_ready):
    s = _series(channel)
    s.is_active = False
    s.save(update_fields=["is_active"])
    _slot(s, asset_ready, dow=0)
    res = expand_series_slots(weeks=4)
    assert res["created"] == 0


# ---- 変則編成パターン (#7 Phase 2 _slot_matches) ----


def _stub(kind, *, dow=0, param=None):
    # 未保存インスタンスで十分 (matches は DB に触れない)
    return SeriesSlot(recurrence_kind=kind, dow=dow, recurrence_param=param or {})


def test_slot_matches_weekly_uses_dow():
    slot = _stub(RecurrenceKind.WEEKLY, dow=0)  # 月曜
    assert _slot_matches(slot, date(2026, 6, 1))  # 月
    assert not _slot_matches(slot, date(2026, 6, 2))  # 火


def test_slot_matches_monthly_nth_dow():
    # 第2・第4 月曜 (2026-06 は 月曜 = 1,8,15,22,29 → 第1=1 第2=8 第3=15 第4=22 第5=29)
    slot = _stub(RecurrenceKind.MONTHLY_NTH_DOW, dow=0, param={"weeks": [2, 4]})
    assert not _slot_matches(slot, date(2026, 6, 1))  # 第1月
    assert _slot_matches(slot, date(2026, 6, 8))  # 第2月
    assert not _slot_matches(slot, date(2026, 6, 15))  # 第3月
    assert _slot_matches(slot, date(2026, 6, 22))  # 第4月
    assert not _slot_matches(slot, date(2026, 6, 9))  # 火曜は対象外


def test_slot_matches_days_of_month():
    slot = _stub(RecurrenceKind.DAYS_OF_MONTH, param={"days": [1, 15]})
    assert _slot_matches(slot, date(2026, 6, 1))
    assert _slot_matches(slot, date(2026, 7, 15))
    assert not _slot_matches(slot, date(2026, 6, 2))


def test_slot_matches_days_ending():
    # 末尾5 = 5/15/25 (毎月)
    slot = _stub(RecurrenceKind.DAYS_ENDING, param={"ending": [5]})
    for d in (5, 15, 25):
        assert _slot_matches(slot, date(2026, 6, d))
    assert not _slot_matches(slot, date(2026, 6, 6))


def test_expand_days_of_month_lands_on_specified_days(channel, asset_ready):
    s = _series(channel)
    sl = _slot(s, asset_ready, dow=0)
    sl.recurrence_kind = RecurrenceKind.DAYS_OF_MONTH
    sl.recurrence_param = {"days": [1, 15]}
    sl.save(update_fields=["recurrence_kind", "recurrence_param"])
    expand_series_slots(weeks=8)  # ~2ヶ月先まで
    days = {
        p.start_at.astimezone(timezone.get_current_timezone()).day
        for p in Program.objects.filter(series=s)
    }
    assert days <= {1, 15} and days  # 1日/15日以外は出ない


def test_upcoming_dates_preview(channel, asset_ready):
    """変則編成プレビュー: 末尾5の日スロットの次回展開日を列挙 (start 固定で決定論)。"""
    s = _series(channel)
    sl = _slot(s, asset_ready, dow=0, eff_from=date(2026, 6, 1))
    sl.recurrence_kind = RecurrenceKind.DAYS_ENDING
    sl.recurrence_param = {"ending": [5]}
    dates = sl.upcoming_dates(n=4, start=date(2026, 6, 1))
    assert dates == [date(2026, 6, 5), date(2026, 6, 15), date(2026, 6, 25), date(2026, 7, 5)]


def test_upcoming_dates_respects_effective_to(channel, asset_ready):
    s = _series(channel)
    sl = _slot(s, asset_ready, dow=0, eff_from=date(2026, 6, 1), eff_to=date(2026, 6, 20))
    sl.recurrence_kind = RecurrenceKind.DAYS_ENDING
    sl.recurrence_param = {"ending": [5]}
    dates = sl.upcoming_dates(n=10, start=date(2026, 6, 1))
    assert dates == [date(2026, 6, 5), date(2026, 6, 15)]  # 6/25 は effective_to 超過


def test_expand_applies_default_asset_cuesheet(channel, asset_ready):
    """既定素材にキューシートがあると展開時に CM枠 を自動付与し end_at を総尺で算出 (#7 Phase 2)。"""
    from medialib.models import CueKind, CuePoint, CueSheet

    # asset_ready は 60分(3_600_000ms)。本編30分 + CM枠30秒 + 本編30分 の基本キューシート
    cue = CueSheet.objects.create(asset=asset_ready)
    CuePoint.objects.create(cue_sheet=cue, seq=0, kind=CueKind.CONTENT, duration_ms=1_800_000)
    CuePoint.objects.create(
        cue_sheet=cue, seq=1, kind=CueKind.AD_BREAK, duration_ms=30_000, grid="15s"
    )
    CuePoint.objects.create(cue_sheet=cue, seq=2, kind=CueKind.CONTENT, duration_ms=1_800_000)

    s = _series(channel)
    _slot(s, asset_ready, dow=0)  # 毎週月曜
    res = expand_series_slots(weeks=2)
    assert res["created"] >= 1
    prog = Program.objects.filter(series=s).first()
    # CM枠 が 1 つ自動生成され、offset=本編頭30分・尺30秒
    breaks = list(prog.ad_breaks.all())
    assert len(breaks) == 1
    assert breaks[0].offset_ms == 1_800_000 and breaks[0].duration_ms == 30_000
    # end_at = start + 素材尺 + CM枠尺 (3_630_000ms)
    assert (prog.end_at - prog.start_at).total_seconds() * 1000 == 3_630_000


def test_expand_without_cuesheet_uses_slot_duration(channel, asset_ready):
    """キューシート未設定なら従来どおり slot.duration_ms で展開し CM枠なし。"""
    s = _series(channel)
    _slot(s, asset_ready, dow=0, dur_ms=1_800_000)
    expand_series_slots(weeks=2)
    prog = Program.objects.filter(series=s).first()
    assert prog.ad_breaks.count() == 0
    assert (prog.end_at - prog.start_at).total_seconds() * 1000 == 1_800_000


# ---- 回別素材の優先採用 (#5 Episode → expand) ----


def _ready_asset(title, dur_ms):
    from medialib.models import Asset, AssetKind, NormalizeStatus

    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title=title,
        duration_ms=dur_ms,
        r2_key=f"mezzanine/program/{title}.mp4",
        normalize_status=NormalizeStatus.READY,
    )


def _next_dow(dow):
    """今日以降で曜日 dow に最初に一致する日付 (展開対象の決定論的な特定回)。"""
    d = timezone.now().date()
    while d.weekday() != dow:
        d += timedelta(days=1)
    return d


def test_expand_links_episode_and_prefers_its_asset(channel, asset_ready):
    s = _series(channel)
    _slot(s, asset_ready, dow=0)  # 毎週月曜, 既定=asset_ready
    ep_asset = _ready_asset("ep-special", 1_200_000)
    d = _next_dow(0)
    ep = Episode.objects.create(
        series=s, episode_no=5, air_date=d, asset=ep_asset, status=EpisodeStatus.CONFIRMED
    )
    expand_series_slots(weeks=2)
    target = Program.objects.get(episode=ep)
    assert target.asset_id == ep_asset.id  # 回別素材を優先
    # 他日 (あれば) は既定素材・episode 未リンク
    for p in Program.objects.filter(series=s).exclude(pk=target.pk):
        assert p.asset_id == asset_ready.id and p.episode_id is None


def test_expand_without_episode_uses_default_asset(channel, asset_ready):
    s = _series(channel)
    _slot(s, asset_ready, dow=0)
    expand_series_slots(weeks=2)
    progs = Program.objects.filter(series=s)
    assert progs.exists()
    for p in progs:
        assert p.episode_id is None and p.asset_id == asset_ready.id


def test_expand_episode_cuesheet_drives_end_at(channel, asset_ready):
    """回別素材にキューシートがあれば end_at はその総尺で算出 (slot 既定でなく)。"""
    from medialib.models import CueKind, CuePoint, CueSheet

    s = _series(channel)
    _slot(s, asset_ready, dow=0, dur_ms=1_800_000)  # slot 既定尺=30分
    ep_asset = _ready_asset("ep-cue", 1_200_000)
    cue = CueSheet.objects.create(asset=ep_asset)
    CuePoint.objects.create(cue_sheet=cue, seq=0, kind=CueKind.CONTENT, duration_ms=1_200_000)
    d = _next_dow(0)
    ep = Episode.objects.create(series=s, episode_no=1, air_date=d, asset=ep_asset)
    expand_series_slots(weeks=2)
    target = Program.objects.get(episode=ep)
    # end_at は回別素材のキューシート総尺 (20分) であり slot 既定 (30分) ではない
    assert (target.end_at - target.start_at).total_seconds() * 1000 == 1_200_000


def test_expand_ignores_episode_asset_not_ready(channel, asset_ready):
    """正規化未完 (READY でない) 回別素材は採用しない → 既定素材で展開。"""
    from medialib.models import Asset, AssetKind, NormalizeStatus

    s = _series(channel)
    _slot(s, asset_ready, dow=0)
    pending = Asset.objects.create(
        kind=AssetKind.PROGRAM, title="ep-pending", normalize_status=NormalizeStatus.PENDING
    )
    d = _next_dow(0)
    Episode.objects.create(series=s, episode_no=2, air_date=d, asset=pending)
    expand_series_slots(weeks=2)
    # 該当日も既定素材・episode 未リンク (READY でないため優先対象外)
    for p in Program.objects.filter(series=s):
        assert p.asset_id == asset_ready.id and p.episode_id is None


def test_expand_idempotent_with_episode(channel, asset_ready):
    s = _series(channel)
    _slot(s, asset_ready, dow=0)
    ep_asset = _ready_asset("ep-idem", 1_200_000)
    d = _next_dow(0)
    Episode.objects.create(series=s, episode_no=3, air_date=d, asset=ep_asset)
    first = expand_series_slots(weeks=2)
    assert first["created"] >= 1
    second = expand_series_slots(weeks=2)  # 再実行
    assert second["created"] == 0 and second["skipped"] == first["created"]
    assert Episode.objects.filter(series=s).count() == 1  # 重複 Episode を作らない


def test_expand_live_slot_never_links_episode(channel, asset_ready):
    """live スロットは回別素材を絡めない (chk_program_source: live は asset NULL)。"""
    from core.models import LiveSource

    s = _series(channel)
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    SeriesSlot.objects.create(
        series=s,
        dow=0,
        start_time="20:00",
        duration_ms=1_800_000,
        program_type=ProgramType.LIVE,
        live_source=ls,
        effective_from=date(2020, 1, 1),
    )
    ep_asset = _ready_asset("ep-stray", 1_200_000)
    d = _next_dow(0)
    Episode.objects.create(series=s, episode_no=9, air_date=d, asset=ep_asset)
    expand_series_slots(weeks=2)
    for p in Program.objects.filter(series=s):
        assert p.type == ProgramType.LIVE and p.asset_id is None and p.episode_id is None


def _live_slot(series, hhmm="20:00", dur_ms=1_800_000):
    from core.models import LiveSource

    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    return SeriesSlot.objects.create(
        series=series,
        dow=0,
        start_time=hhmm,
        duration_ms=dur_ms,
        program_type=ProgramType.LIVE,
        live_source=ls,
        effective_from=date(2020, 1, 1),
    )


def test_expand_copies_live_rundown_template(channel, asset_ready):
    """定番進行表 (LiveRundownTemplate) を持つ生スロットは展開先 Program に
    LiveRundown/LiveCue を state=pending で複製する (#25 Phase3 C §9)。"""
    from medialib.models import CmBundle
    from scheduling.models import (
        LiveCue,
        LiveCueKind,
        LiveCueState,
        LiveRundown,
        LiveRundownTemplate,
        LiveRundownTemplateCue,
    )

    s = _series(channel)
    slot = _live_slot(s)
    bundle = CmBundle.objects.create(name="束A")
    tmpl = LiveRundownTemplate.objects.create(slot=slot)
    LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=1, kind=LiveCueKind.SECTION, label="OP", planned_duration_ms=300_000
    )
    LiveRundownTemplateCue.objects.create(
        template=tmpl,
        seq=2,
        kind=LiveCueKind.CM,
        label="CM",
        planned_duration_ms=30_000,
        cm_bundle=bundle,
        grid="15s",
        auto_fire=True,
        auto_offset_ms=600_000,
    )
    LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=3, kind=LiveCueKind.VT, planned_duration_ms=60_000, asset=asset_ready
    )

    expand_series_slots(weeks=2)
    progs = list(Program.objects.filter(series=s, type=ProgramType.LIVE))
    assert progs, "生 Program が展開されていない"
    for p in progs:
        rd = LiveRundown.objects.get(program=p)  # 各回に rundown が付く
        cues = list(rd.cues.order_by("seq"))
        assert [c.kind for c in cues] == [LiveCueKind.SECTION, LiveCueKind.CM, LiveCueKind.VT]
        assert all(c.state == LiveCueState.PENDING and c.fired_event_id is None for c in cues)
        cm = cues[1]
        assert cm.cm_bundle_id == bundle.id and cm.grid == "15s"
        assert cm.auto_fire is True and cm.auto_offset_ms == 600_000
        assert cues[2].asset_id == asset_ready.id
    # cue 総数 = 生 Program 数 × 3
    assert LiveCue.objects.filter(rundown__program__series=s).count() == len(progs) * 3


def test_expand_live_slot_without_template_makes_no_rundown(channel):
    """雛形の無い生スロットは LiveRundown を作らない (従来挙動不変)。"""
    from scheduling.models import LiveRundown

    s = _series(channel)
    _live_slot(s)
    expand_series_slots(weeks=2)
    progs = Program.objects.filter(series=s, type=ProgramType.LIVE)
    assert progs.exists()
    assert not LiveRundown.objects.filter(program__series=s).exists()


def test_recurrence_label_renders_each_kind(channel, asset_ready):
    s = _series(channel)
    weekly = _slot(s, asset_ready, dow=0)
    assert weekly.recurrence_label() == "毎週月"
    nth = _slot(s, asset_ready, dow=2)
    nth.recurrence_kind = RecurrenceKind.MONTHLY_NTH_DOW
    nth.recurrence_param = {"weeks": [2, 4]}
    assert "第2" in nth.recurrence_label() and "水" in nth.recurrence_label()
    dom = _slot(s, asset_ready, dow=0)
    dom.recurrence_kind = RecurrenceKind.DAYS_OF_MONTH
    dom.recurrence_param = {"days": [1, 15]}
    assert "15日" in dom.recurrence_label()
    end = _slot(s, asset_ready, dow=0)
    end.recurrence_kind = RecurrenceKind.DAYS_ENDING
    end.recurrence_param = {"ending": [5]}
    assert "末尾5" in end.recurrence_label()
