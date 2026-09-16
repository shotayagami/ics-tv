# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 タイムライン編集 API: move/resize/validate + ad_break。EXCLUSION/grid 検証。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scheduling.models import AdBreak, CmGrid, Program, ProgramType

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _staff_login(client, staff_user):
    # 編成 API は staff_member_required (#7 セキュリティ)。全テストをスタッフ認証で実行。
    client.force_login(staff_user)


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _recorded(channel, asset_ready, start, dur_ms=3_600_000, title="P"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=start + timedelta(milliseconds=dur_ms),
        asset=asset_ready,
    )


def _live(channel, start, end, title="L"):
    from core.models import LiveSource

    ls = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end,
        live_source=ls,
    )


def _url(name, channel, **kw):
    from django.urls import reverse

    return reverse(f"scheduling:{name}", kwargs={"slug": channel.slug, **kw})


# ---- move ----


def test_move_to_free_slot(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    new_start = BASE + timedelta(hours=3)
    res = _post(
        client, _url("program_move", channel, program_id=p.id), {"start_at": new_start.isoformat()}
    )
    assert res.status_code == 200
    p.refresh_from_db()
    assert p.start_at == new_start
    assert p.end_at == new_start + timedelta(milliseconds=3_600_000)  # 尺保持


def test_move_overlap_returns_422_with_earliest(client, channel, asset_ready, db):
    _recorded(channel, asset_ready, BASE, title="A")  # 12:00-13:00 (重なり相手)
    b = _recorded(channel, asset_ready, BASE + timedelta(hours=2), title="B")  # 14:00-15:00
    # B を A に重なる 12:30 へ → 422 + 最早 (A の終わり 13:00)
    res = _post(
        client,
        _url("program_move", channel, program_id=b.id),
        {"start_at": (BASE + timedelta(minutes=30)).isoformat()},
    )
    assert res.status_code == 422
    body = res.json()
    assert body["ok"] is False
    assert body["earliest"].startswith("2026-07-01T13:00")
    b.refresh_from_db()
    assert b.start_at == BASE + timedelta(hours=2)  # 変更されない


def test_validate_ok_and_conflict(client, channel, asset_ready, db):
    _recorded(channel, asset_ready, BASE, title="A")  # 重なり相手
    b = _recorded(channel, asset_ready, BASE + timedelta(hours=2), title="B")
    ok = _post(
        client,
        _url("program_validate", channel),
        {"program_id": b.id, "start_at": (BASE + timedelta(hours=5)).isoformat()},
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    conflict = _post(
        client,
        _url("program_validate", channel),
        {"program_id": b.id, "start_at": BASE.isoformat()},
    )
    assert conflict.status_code == 422


# ---- resize ----


def test_resize_live_changes_end(client, channel, db):
    lv = _live(channel, BASE, BASE + timedelta(hours=1))
    new_end = BASE + timedelta(hours=2)
    res = _post(
        client, _url("program_resize", channel, program_id=lv.id), {"end_at": new_end.isoformat()}
    )
    assert res.status_code == 200
    lv.refresh_from_db()
    assert lv.end_at == new_end


def test_resize_recorded_rejected(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    res = _post(
        client,
        _url("program_resize", channel, program_id=p.id),
        {"end_at": (BASE + timedelta(hours=2)).isoformat()},
    )
    assert res.status_code == 422  # 録画は尺固定


# ---- ad_break ----


def test_adbreak_create_valid(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    res = _post(
        client,
        _url("adbreak_create", channel, program_id=p.id),
        {"offset_ms": 600000, "grid": "15s", "duration_ms": 30000},
    )
    assert res.status_code == 200
    assert AdBreak.objects.filter(program=p, offset_ms=600000).exists()


def test_adbreak_create_rejects_non_grid_multiple(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    res = _post(
        client,
        _url("adbreak_create", channel, program_id=p.id),
        {"offset_ms": 600000, "grid": "15s", "duration_ms": 20000},
    )
    assert res.status_code == 422  # 20000 は 15000 の整数倍でない
    assert not AdBreak.objects.exists()


def test_adbreak_create_rejects_offset_out_of_range(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    res = _post(
        client,
        _url("adbreak_create", channel, program_id=p.id),
        {"offset_ms": 9_999_999, "grid": "15s", "duration_ms": 15000},
    )
    assert res.status_code == 422  # 素材尺 (1h) 範囲外


def test_adbreak_move_and_delete(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    br = AdBreak.objects.create(program=p, offset_ms=600000, grid=CmGrid.G15, duration_ms=15000)
    mv = _post(client, _url("adbreak_update", channel, adbreak_id=br.id), {"offset_ms": 1_200_000})
    assert mv.status_code == 200
    br.refresh_from_db()
    assert br.offset_ms == 1_200_000
    dl = client.post(_url("adbreak_delete", channel, adbreak_id=br.id))
    assert dl.status_code == 204
    assert not AdBreak.objects.filter(pk=br.id).exists()


# ---- end_at 不変条件 (CM枠は尺を足す) ----


def test_adbreak_create_extends_end_at(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)  # 1h
    res = _post(
        client,
        _url("adbreak_create", channel, program_id=p.id),
        {"offset_ms": 600000, "grid": "15s", "duration_ms": 30000},
    )
    assert res.status_code == 200
    p.refresh_from_db()
    assert p.end_at == BASE + timedelta(milliseconds=3_600_000 + 30000)


def test_adbreak_create_rejects_when_extension_overlaps(client, channel, asset_ready, db):
    a = _recorded(channel, asset_ready, BASE, title="A")  # 12:00-13:00
    _recorded(channel, asset_ready, BASE + timedelta(hours=1), title="B")  # 13:00-14:00 (隣接)
    res = _post(
        client,
        _url("adbreak_create", channel, program_id=a.id),
        {"offset_ms": 600000, "grid": "15s", "duration_ms": 30000},
    )
    assert res.status_code == 422  # 30s 伸びると B に食い込む
    assert not AdBreak.objects.filter(program=a).exists()


def test_adbreak_delete_shrinks_end_at(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    _post(
        client,
        _url("adbreak_create", channel, program_id=p.id),
        {"offset_ms": 600000, "grid": "15s", "duration_ms": 30000},
    )
    p.refresh_from_db()
    br = AdBreak.objects.get(program=p)
    client.post(_url("adbreak_delete", channel, adbreak_id=br.id))
    p.refresh_from_db()
    assert p.end_at == BASE + timedelta(milliseconds=3_600_000)


# ---- キューシート適用 ----


def _build_cuesheet(asset):
    """asset (1h) に 本編30分 → CM60s → 本編30分 のキューシートを作る。"""
    from medialib.models import CmGrid as MlGrid
    from medialib.models import CueKind, CuePoint, CueSheet

    cue = CueSheet.objects.create(asset=asset)
    CuePoint.objects.create(cue_sheet=cue, seq=1, kind=CueKind.CONTENT, duration_ms=1_800_000)
    CuePoint.objects.create(
        cue_sheet=cue, seq=2, kind=CueKind.AD_BREAK, duration_ms=60_000, grid=MlGrid.G15
    )
    CuePoint.objects.create(cue_sheet=cue, seq=3, kind=CueKind.CONTENT, duration_ms=1_800_000)
    return cue


def test_apply_cuesheet_materializes_breaks_and_extends_end(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    _build_cuesheet(asset_ready)
    res = _post(client, _url("adbreak_apply_cuesheet", channel, program_id=p.id), {})
    assert res.status_code == 200
    assert json.loads(res.content)["n_breaks"] == 1
    br = AdBreak.objects.get(program=p)
    assert br.offset_ms == 1_800_000
    assert br.duration_ms == 60_000
    p.refresh_from_db()
    assert p.end_at == BASE + timedelta(milliseconds=3_600_000 + 60_000)


def test_apply_cuesheet_replaces_existing_breaks(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    AdBreak.objects.create(program=p, offset_ms=10_000, grid=CmGrid.G15, duration_ms=15000)
    _build_cuesheet(asset_ready)
    res = _post(client, _url("adbreak_apply_cuesheet", channel, program_id=p.id), {})
    assert res.status_code == 200
    # 既存枠は置換され、キューシート由来の 1 枠だけ残る
    assert AdBreak.objects.filter(program=p).count() == 1
    assert AdBreak.objects.get(program=p).offset_ms == 1_800_000


def test_apply_cuesheet_without_sheet_422(client, channel, asset_ready, db):
    p = _recorded(channel, asset_ready, BASE)
    res = _post(client, _url("adbreak_apply_cuesheet", channel, program_id=p.id), {})
    assert res.status_code == 422


def test_apply_cuesheet_overlap_422(client, channel, asset_ready, db):
    a = _recorded(channel, asset_ready, BASE, title="A")  # 12:00-13:00
    _recorded(channel, asset_ready, BASE + timedelta(hours=1), title="B")  # 隣接
    _build_cuesheet(asset_ready)
    res = _post(client, _url("adbreak_apply_cuesheet", channel, program_id=a.id), {})
    assert res.status_code == 422
    assert not AdBreak.objects.filter(program=a).exists()


# ---- CM枠への CM 手動割当 (program_breaks + AdBreakItem add/delete) ----


def test_adbreak_item_add_and_delete(client, channel, asset_ready, db):
    from medialib.models import Asset, AssetKind, CmCreative, NormalizeStatus
    from medialib.models import CmGrid as MlGrid
    from scheduling.models import AdBreakItem

    p = _recorded(channel, asset_ready, BASE)
    br = AdBreak.objects.create(program=p, offset_ms=600000, grid=CmGrid.G15, duration_ms=30000)
    cm_asset = Asset.objects.create(
        kind=AssetKind.CM, title="CM A", duration_ms=15000, normalize_status=NormalizeStatus.READY
    )
    cm = CmCreative.objects.create(asset=cm_asset, advertiser="広告主A", grid=MlGrid.G15)

    # 割当ページ描画
    res = client.get(_url("program_breaks", channel, program_id=p.id))
    assert res.status_code == 200

    # 枠に CM を手動割当
    add = client.post(reverse_item("adbreak_item_add", adbreak_id=br.id), {"cm_id": cm.asset_id})
    assert add.status_code == 302
    item = AdBreakItem.objects.get(ad_break=br)
    assert item.cm_asset_id == cm.asset_id

    # 外す
    dl = client.post(reverse_item("adbreak_item_delete", item_id=item.id))
    assert dl.status_code == 302
    assert not AdBreakItem.objects.filter(pk=item.id).exists()


def test_adbreak_item_add_rejects_grid_mismatch(client, channel, asset_ready, db):
    from medialib.models import Asset, AssetKind, CmCreative, NormalizeStatus
    from medialib.models import CmGrid as MlGrid
    from scheduling.models import AdBreakItem

    p = _recorded(channel, asset_ready, BASE)
    br = AdBreak.objects.create(program=p, offset_ms=600000, grid=CmGrid.G15, duration_ms=30000)
    cm_asset = Asset.objects.create(
        kind=AssetKind.CM, title="CM 20s", duration_ms=20000, normalize_status=NormalizeStatus.READY
    )
    cm = CmCreative.objects.create(asset=cm_asset, advertiser="B", grid=MlGrid.G20)  # grid 不一致
    client.post(reverse_item("adbreak_item_add", adbreak_id=br.id), {"cm_id": cm.asset_id})
    assert not AdBreakItem.objects.filter(ad_break=br).exists()  # grid 不一致は割当されない


def reverse_item(name, **kw):
    from django.urls import reverse

    return reverse(f"scheduling:{name}", kwargs=kw)
