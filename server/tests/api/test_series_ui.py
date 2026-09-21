# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""週間基本編成 (Series / SeriesSlot) UI + 手動展開 (#6 Phase C / Phase 2)。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from django.urls import reverse

from scheduling.models import Program, Series, SeriesSlot


@pytest.fixture(autouse=True)
def _staff_login(client, staff_user):
    # 週間編成 UI は staff_member_required (#7 セキュリティ)。全テストをスタッフ認証で実行。
    client.force_login(staff_user)


def _u(name, channel, **kw):
    return reverse(f"scheduling:{name}", kwargs={"slug": channel.slug, **kw})


def _prog(channel, h, asset=None, series=None):
    start = datetime(2026, 6, 20, h, 0, tzinfo=UTC)
    return Program.objects.create(
        channel=channel,
        series=series,
        type="recorded",
        title="番組",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
    )


def test_program_thumb_url_prefers_asset_then_series(channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="番組", thumbnail_url="https://x/series.png")
    # 素材サムネ優先
    asset_ready.thumbnail_url = "https://x/asset.png"
    asset_ready.save(update_fields=["thumbnail_url"])
    assert _prog(channel, 10, asset=asset_ready, series=s).thumb_url == "https://x/asset.png"
    # 素材サムネ無 → シリーズへフォールバック
    asset_ready.thumbnail_url = None
    asset_ready.save(update_fields=["thumbnail_url"])
    assert _prog(channel, 12, asset=asset_ready, series=s).thumb_url == "https://x/series.png"
    # どちらも無 → 空 (テンプレが固定プレースホルダを描画)
    s.thumbnail_url = None
    s.save(update_fields=["thumbnail_url"])
    assert _prog(channel, 14, asset=asset_ready).thumb_url == ""


def test_series_list_renders(client, channel, db):
    res = client.get(_u("series_list", channel))
    assert res.status_code == 200
    assert "週間基本編成" in res.content.decode("utf-8")


def test_create_series_then_add_recorded_slot(client, channel, asset_ready, db):
    # シリーズ作成 → series_edit へ
    res = client.post(_u("series_list", channel), {"title": "朝の番組", "is_active": "on"})
    assert res.status_code == 302
    s = Series.objects.get(channel=channel, title="朝の番組")

    # 録画スロット追加 (尺は分入力 → duration_ms 換算)
    res = client.post(
        _u("slot_add", channel, series_id=s.id),
        {
            "dow": "0",
            "start_time": "07:00",
            "duration_min": "30",
            "program_type": "recorded",
            "default_asset": asset_ready.id,
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 302
    slot = SeriesSlot.objects.get(series=s)
    assert slot.dow == 0 and slot.duration_ms == 30 * 60000 and slot.start_time == time(7, 0)


def test_series_edit_page_renders_with_cue_column(client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="編集ページ")
    SeriesSlot.objects.create(
        series=s,
        dow=0,
        start_time="07:00",
        duration_ms=30 * 60000,
        program_type="recorded",
        default_asset=asset_ready,
        effective_from=date(2026, 6, 1),
    )
    res = client.get(_u("series_edit", channel, series_id=s.id))
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "キュー" in body and "繰り返し" in body  # キュー列 + 変則編成セレクト


def test_recorded_slot_requires_default_asset(client, channel, db):
    s = Series.objects.create(channel=channel, title="x")
    res = client.post(
        _u("slot_add", channel, series_id=s.id),
        {
            "dow": "1",
            "start_time": "08:00",
            "duration_min": "30",
            "program_type": "recorded",
            "effective_from": "2026-06-01",
        },  # default_asset 欠落
    )
    assert res.status_code == 200  # 再描画 (エラー)
    assert not SeriesSlot.objects.filter(series=s).exists()


def test_add_monthly_nth_dow_slot_builds_param(client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="第2・第4 番組")
    res = client.post(
        _u("slot_add", channel, series_id=s.id),
        {
            "recurrence_kind": "monthly_nth_dow",
            "dow": "0",
            "weeks_csv": "2,4",
            "start_time": "21:00",
            "duration_min": "60",
            "program_type": "recorded",
            "default_asset": asset_ready.id,
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 302
    slot = SeriesSlot.objects.get(series=s)
    assert slot.recurrence_kind == "monthly_nth_dow"
    assert slot.recurrence_param == {"weeks": [2, 4]}


def test_add_days_ending_slot_builds_param(client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="5の日")
    res = client.post(
        _u("slot_add", channel, series_id=s.id),
        {
            "recurrence_kind": "days_ending",
            "dow": "0",
            "ending_csv": "5",
            "start_time": "12:00",
            "duration_min": "30",
            "program_type": "recorded",
            "default_asset": asset_ready.id,
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 302
    slot = SeriesSlot.objects.get(series=s)
    assert slot.recurrence_kind == "days_ending" and slot.recurrence_param == {"ending": [5]}


def test_monthly_nth_dow_without_weeks_rejected(client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="不正")
    res = client.post(
        _u("slot_add", channel, series_id=s.id),
        {
            "recurrence_kind": "monthly_nth_dow",
            "dow": "0",
            "weeks_csv": "",  # 必須欠落
            "start_time": "21:00",
            "duration_min": "60",
            "program_type": "recorded",
            "default_asset": asset_ready.id,
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 200  # 再描画
    assert not SeriesSlot.objects.filter(series=s).exists()


def test_expand_now_creates_programs(client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="毎日番組", is_active=True)
    # 全曜日ぶん置くと 4 週で必ず複数生成される
    for dow in range(7):
        SeriesSlot.objects.create(
            series=s,
            dow=dow,
            start_time=time(3, 0),
            duration_ms=30 * 60000,
            program_type="recorded",
            default_asset=asset_ready,
            effective_from=date(2020, 1, 1),
        )
    res = client.post(_u("series_expand", channel))
    assert res.status_code == 302
    assert "expanded=" in res.url
    assert Program.objects.filter(series=s).exists()  # スロットから番組が生成された
