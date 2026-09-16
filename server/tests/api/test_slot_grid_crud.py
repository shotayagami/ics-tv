# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""週間グリッドの SeriesSlot D&D 編集 API (edit_views.slot_create_grid/move/resize)。

plain Django JSON view (postJson)。staff 限定・SeriesSlotForm の検証再利用 (録画→素材/生→ソース)。
スロット移動 (dow/start_time)・尺変更 (duration_ms)。
"""

from __future__ import annotations

import json
from datetime import date, time

import pytest
from django.urls import reverse

from scheduling.models import ProgramType, RecurrenceKind, Series, SeriesSlot


@pytest.fixture(autouse=True)
def _staff_login(client, staff_user):
    client.force_login(staff_user)


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _url(name, channel, **kw):
    return reverse(f"scheduling:{name}", kwargs={"slug": channel.slug, **kw})


def _series(channel):
    return Series.objects.create(channel=channel, title="X")


def test_create_recorded_slot(client, channel, asset_ready, db):
    s = _series(channel)
    res = _post(
        client,
        _url("slot_create_grid", channel),
        {
            "series_id": s.id,
            "dow": 2,
            "start_time": "19:00",
            "duration_min": 30,
            "program_type": "recorded",
            "default_asset_id": asset_ready.id,
            "recurrence_kind": "weekly",
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    slot = SeriesSlot.objects.get(pk=body["slot_id"])
    assert slot.series_id == s.id and slot.dow == 2
    assert slot.duration_ms == 1_800_000 and slot.start_time == time(19, 0)
    assert slot.recurrence_kind == RecurrenceKind.WEEKLY


def test_create_recorded_without_asset_422(client, channel, db):
    s = _series(channel)
    res = _post(
        client,
        _url("slot_create_grid", channel),
        {
            "series_id": s.id,
            "dow": 2,
            "start_time": "19:00",
            "duration_min": 30,
            "program_type": "recorded",
            "recurrence_kind": "weekly",
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 422
    assert not SeriesSlot.objects.exists()


def test_create_live_without_source_422(client, channel, db):
    s = _series(channel)
    res = _post(
        client,
        _url("slot_create_grid", channel),
        {
            "series_id": s.id,
            "dow": 2,
            "start_time": "19:00",
            "duration_min": 30,
            "program_type": "live",
            "recurrence_kind": "weekly",
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 422
    assert not SeriesSlot.objects.exists()


def test_create_live_with_source(client, channel, db):
    from core.models import LiveSource

    s = _series(channel)
    ls = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")
    res = _post(
        client,
        _url("slot_create_grid", channel),
        {
            "series_id": s.id,
            "dow": 0,
            "start_time": "20:00",
            "duration_min": 60,
            "program_type": "live",
            "live_source_id": ls.id,
            "recurrence_kind": "weekly",
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 200
    slot = SeriesSlot.objects.get(pk=res.json()["slot_id"])
    assert slot.program_type == ProgramType.LIVE and slot.live_source_id == ls.id


def test_create_monthly_nth_dow_recurrence_param(client, channel, asset_ready, db):
    s = _series(channel)
    res = _post(
        client,
        _url("slot_create_grid", channel),
        {
            "series_id": s.id,
            "dow": 0,
            "start_time": "19:00",
            "duration_min": 30,
            "program_type": "recorded",
            "default_asset_id": asset_ready.id,
            "recurrence_kind": "monthly_nth_dow",
            "weeks_csv": "2,4",
            "effective_from": "2026-06-01",
        },
    )
    assert res.status_code == 200
    slot = SeriesSlot.objects.get(pk=res.json()["slot_id"])
    assert slot.recurrence_kind == RecurrenceKind.MONTHLY_NTH_DOW
    assert slot.recurrence_param == {"weeks": [2, 4]}


def _make_slot(channel, asset):
    s = _series(channel)
    return SeriesSlot.objects.create(
        series=s,
        dow=0,
        start_time=time(19, 0),
        duration_ms=1_800_000,
        program_type=ProgramType.RECORDED,
        default_asset=asset,
        effective_from=date(2026, 1, 1),
    )


def test_move_changes_dow_and_time(client, channel, asset_ready, db):
    slot = _make_slot(channel, asset_ready)
    res = _post(
        client, _url("slot_move_grid", channel, slot_id=slot.id), {"dow": 3, "start_time": "21:30"}
    )
    assert res.status_code == 200
    slot.refresh_from_db()
    assert slot.dow == 3 and slot.start_time == time(21, 30)


def test_move_rejects_bad_dow(client, channel, asset_ready, db):
    slot = _make_slot(channel, asset_ready)
    res = _post(client, _url("slot_move_grid", channel, slot_id=slot.id), {"dow": 9})
    assert res.status_code == 400
    slot.refresh_from_db()
    assert slot.dow == 0


def test_resize_changes_duration(client, channel, asset_ready, db):
    slot = _make_slot(channel, asset_ready)
    res = _post(
        client, _url("slot_resize_grid", channel, slot_id=slot.id), {"duration_ms": 3_600_000}
    )
    assert res.status_code == 200
    slot.refresh_from_db()
    assert slot.duration_ms == 3_600_000


def test_resize_rejects_non_positive(client, channel, asset_ready, db):
    slot = _make_slot(channel, asset_ready)
    res = _post(client, _url("slot_resize_grid", channel, slot_id=slot.id), {"duration_ms": 0})
    assert res.status_code == 400


def test_slot_endpoints_require_staff(channel, asset_ready, db):
    """匿名 (未ログイン) は staff_member_required で弾かれる (リダイレクト)。"""
    from django.test import Client

    slot = _make_slot(channel, asset_ready)
    anon = Client()
    res = anon.post(
        _url("slot_resize_grid", channel, slot_id=slot.id),
        data=json.dumps({"duration_ms": 60000}),
        content_type="application/json",
    )
    assert res.status_code in (302, 403)
