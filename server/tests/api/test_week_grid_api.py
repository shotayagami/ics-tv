# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""週間グリッド編成 読み API: 当週の実 Program + 基本編成スロット投影 (matches/effective/covered)。

GET /admin/scheduling/{slug}/week?start=YYYY-MM-DD。月曜スナップ・曜日列割当 (localtime)・
SeriesSlot を 7 日へ投影 (RecurrenceKind 各種) を検証する。書き込みは test_slot_grid_crud。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from scheduling.models import (
    Program,
    ProgramType,
    RecurrenceKind,
    Series,
    SeriesSlot,
)

WEEK = "/api/v1/admin/scheduling/{slug}/week"
MON = date(2026, 6, 1)  # 2026-06-01 は月曜 (列 0)


def _series(channel, title="番組", active=True):
    return Series.objects.create(channel=channel, title=title, is_active=active)


def _slot(
    series,
    asset,
    dow,
    *,
    hhmm=time(19, 0),
    dur_ms=1_800_000,
    kind=RecurrenceKind.WEEKLY,
    param=None,
    eff_from=date(2020, 1, 1),
    eff_to=None,
):
    return SeriesSlot.objects.create(
        series=series,
        dow=dow,
        start_time=hhmm,
        duration_ms=dur_ms,
        program_type=ProgramType.RECORDED,
        default_asset=asset,
        recurrence_kind=kind,
        recurrence_param=param or {},
        effective_from=eff_from,
        effective_to=eff_to,
    )


def _aware(d, t=time(19, 0)):
    return timezone.make_aware(datetime.combine(d, t))


def _get(staff_client, channel, start):
    return staff_client.get(WEEK.format(slug=channel.slug) + f"?start={start}").json()


def test_week_requires_auth(http_client, channel, db):
    assert http_client.get(WEEK.format(slug=channel.slug)).status_code == 401


def test_week_snaps_to_monday_and_returns_7_days(staff_client, channel, db):
    # 水曜 (2026-06-03) を渡しても週頭は月曜 (2026-06-01)
    d = _get(staff_client, channel, "2026-06-03")
    assert d["week_start"] == "2026-06-01"
    assert d["days"] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
        "2026-06-05",
        "2026-06-06",
        "2026-06-07",
    ]


def test_weekly_slot_projects_to_its_column(staff_client, channel, asset_ready, db):
    s = _series(channel)
    _slot(s, asset_ready, dow=2)  # 毎週水曜
    d = _get(staff_client, channel, "2026-06-01")
    occ = [o for o in d["slots"] if o["series_id"] == s.id]
    assert len(occ) == 1
    assert occ[0]["dow"] == 2 and occ[0]["date"] == "2026-06-03"
    assert occ[0]["start_time"] == "19:00" and occ[0]["duration_ms"] == 1_800_000
    assert occ[0]["covered"] is False


def test_daily_slot_projects_to_all_seven_days(staff_client, channel, asset_ready, db):
    s = _series(channel)
    _slot(s, asset_ready, dow=0, kind=RecurrenceKind.DAILY)
    d = _get(staff_client, channel, "2026-06-01")
    assert sorted(o["dow"] for o in d["slots"] if o["series_id"] == s.id) == [0, 1, 2, 3, 4, 5, 6]


def test_monthly_nth_dow_only_matching_week(staff_client, channel, asset_ready, db):
    s = _series(channel)
    # 第1 月曜のみ → 2026-06-01 (第1月) を含む週でのみ出る
    _slot(s, asset_ready, dow=0, kind=RecurrenceKind.MONTHLY_NTH_DOW, param={"weeks": [1]})
    d = _get(staff_client, channel, "2026-06-01")
    occ = [o for o in d["slots"] if o["series_id"] == s.id]
    assert len(occ) == 1 and occ[0]["date"] == "2026-06-01"
    # 翌週 (第2月 = 06-08) には出ない
    d2 = _get(staff_client, channel, "2026-06-08")
    assert not [o for o in d2["slots"] if o["series_id"] == s.id]


def test_days_of_month_present_only_in_week_containing_date(staff_client, channel, asset_ready, db):
    s = _series(channel)
    _slot(s, asset_ready, dow=0, kind=RecurrenceKind.DAYS_OF_MONTH, param={"days": [15]})
    # 1-7 の週には無い
    assert not [
        o for o in _get(staff_client, channel, "2026-06-01")["slots"] if o["series_id"] == s.id
    ]
    # 15 を含む週 (2026-06-15 は月曜) には dow=0 で出る
    d = _get(staff_client, channel, "2026-06-15")
    occ = [o for o in d["slots"] if o["series_id"] == s.id]
    assert len(occ) == 1 and occ[0]["dow"] == 0 and occ[0]["date"] == "2026-06-15"


def test_effective_window_limits_occurrences(staff_client, channel, asset_ready, db):
    s = _series(channel)
    # 毎日だが水曜 (06-03) から有効 → 水〜日の 5 日だけ
    _slot(s, asset_ready, dow=0, kind=RecurrenceKind.DAILY, eff_from=date(2026, 6, 3))
    d = _get(staff_client, channel, "2026-06-01")
    assert sorted(o["dow"] for o in d["slots"] if o["series_id"] == s.id) == [2, 3, 4, 5, 6]


def test_inactive_series_slots_excluded(staff_client, channel, asset_ready, db):
    s = _series(channel, active=False)
    _slot(s, asset_ready, dow=2)
    d = _get(staff_client, channel, "2026-06-01")
    assert not [o for o in d["slots"] if o["series_id"] == s.id]


def test_covered_when_concrete_program_occupies_slot(staff_client, channel, asset_ready, db):
    s = _series(channel)
    _slot(s, asset_ready, dow=2, hhmm=time(19, 0))  # 水 19:00
    # 同 series の実 Program を 2026-06-03 19:00 に作る → その投影は covered
    start = _aware(date(2026, 6, 3), time(19, 0))
    Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="本番",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        asset=asset_ready,
    )
    d = _get(staff_client, channel, "2026-06-01")
    occ = next(o for o in d["slots"] if o["series_id"] == s.id)
    assert occ["covered"] is True
    # 実 Program は差分レイヤに dow=2 で出る
    prog = next(p for p in d["programs"] if p["title"] == "本番")
    assert prog["dow"] == 2


def test_program_outside_week_excluded(staff_client, channel, asset_ready, db):
    s = _series(channel)
    start = _aware(date(2026, 6, 20), time(12, 0))  # 別週
    Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="別週",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset_ready,
    )
    d = _get(staff_client, channel, "2026-06-01")
    assert all(p["title"] != "別週" for p in d["programs"])
