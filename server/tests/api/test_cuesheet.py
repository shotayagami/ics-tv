# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""キューシート (素材内部ランダウン) のモデル・検証・ad_break 展開 (#7)。"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from medialib import services
from medialib.models import Asset, AssetKind, CmGrid, CueKind, CuePoint, CueSheet


@pytest.fixture
def staff_client(db, client):
    user = get_user_model().objects.create_user("cue_staff", password="x", is_staff=True)
    client.force_login(user)
    return client


def _sheet(duration_ms: int) -> CueSheet:
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="本編", duration_ms=duration_ms)
    return CueSheet.objects.create(asset=asset)


def _content(sheet, seq, dur, label=""):
    return CuePoint.objects.create(
        cue_sheet=sheet, seq=seq, kind=CueKind.CONTENT, duration_ms=dur, label=label
    )


def _cm(sheet, seq, dur, grid=CmGrid.G15):
    return CuePoint.objects.create(
        cue_sheet=sheet, seq=seq, kind=CueKind.AD_BREAK, duration_ms=dur, grid=grid
    )


@pytest.mark.django_db
def test_derive_ad_breaks_computes_offsets():
    # 本編5分 → CM60s → 本編8分 → CM30s → 本編3分 (Σ本編 = 16分 = 素材尺)
    sheet = _sheet(16 * 60_000)
    _content(sheet, 0, 5 * 60_000)
    _cm(sheet, 1, 60_000)
    _content(sheet, 2, 8 * 60_000)
    _cm(sheet, 3, 30_000)
    _content(sheet, 4, 3 * 60_000)

    specs = services.derive_ad_breaks(sheet)
    assert specs == [
        {"offset_ms": 5 * 60_000, "grid": "15s", "duration_ms": 60_000},
        {"offset_ms": 13 * 60_000, "grid": "15s", "duration_ms": 30_000},
    ]
    # 配置時の総尺 = 素材尺 + ΣCM
    assert services.cue_total_airtime_ms(sheet) == 16 * 60_000 + 90_000


@pytest.mark.django_db
def test_validate_rejects_content_sum_mismatch():
    sheet = _sheet(10 * 60_000)
    _content(sheet, 0, 5 * 60_000)  # 本編合計 5 分 ≠ 素材尺 10 分
    with pytest.raises(services.CueSheetError, match="一致しません"):
        services.validate_cue_sheet(sheet)


@pytest.mark.django_db
def test_validate_rejects_non_grid_multiple_cm():
    sheet = _sheet(10 * 60_000)
    _content(sheet, 0, 5 * 60_000)
    _cm(sheet, 1, 20_000, grid=CmGrid.G15)  # 20s は 15s の整数倍でない
    _content(sheet, 2, 5 * 60_000)
    with pytest.raises(services.CueSheetError, match="整数倍"):
        services.validate_cue_sheet(sheet)


@pytest.mark.django_db
def test_validate_rejects_break_at_boundary():
    # 末尾 CM (post-roll): offset == 素材尺 → 挿入モデルでは不可
    sheet = _sheet(10 * 60_000)
    _content(sheet, 0, 10 * 60_000)
    _cm(sheet, 1, 15_000)
    with pytest.raises(services.CueSheetError, match="本編の途中"):
        services.validate_cue_sheet(sheet)


@pytest.mark.django_db
def test_validate_requires_duration():
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="未正規化", duration_ms=None)
    sheet = CueSheet.objects.create(asset=asset)
    _content(sheet, 0, 1000)
    with pytest.raises(services.CueSheetError, match="素材尺"):
        services.validate_cue_sheet(sheet)


# ---- 編集 UI (HTTP) ----


def test_editor_renders(staff_client, db):
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組X", duration_ms=600_000)
    res = staff_client.get(f"/medialib/asset/{asset.id}/cuesheet/")
    assert res.status_code == 200
    assert "番組X" in res.content.decode("utf-8")


def test_cuepoint_add_creates_sheet_and_point(staff_client, db):
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組X", duration_ms=600_000)
    res = staff_client.post(
        f"/medialib/asset/{asset.id}/cuesheet/add/",
        {"kind": "content", "min": "5", "sec": "0", "label": "Aパート"},
    )
    assert res.status_code == 200
    cue = CueSheet.objects.get(asset=asset)
    p = cue.points.get()
    assert p.kind == CueKind.CONTENT
    assert p.duration_ms == 5 * 60_000
    assert p.label == "Aパート"


def test_cuepoint_add_adbreak_requires_grid(staff_client, db):
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組X", duration_ms=600_000)
    res = staff_client.post(
        f"/medialib/asset/{asset.id}/cuesheet/add/",
        {"kind": "ad_break", "min": "0", "sec": "30"},  # grid 欠落
    )
    assert res.status_code == 400
    assert not CuePoint.objects.exists()


def test_cuepoint_move_swaps_order(staff_client, db):
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組X", duration_ms=600_000)
    cue = CueSheet.objects.create(asset=asset)
    a = _content(cue, 1, 60_000, label="first")
    b = _content(cue, 2, 60_000, label="second")
    res = staff_client.post(f"/medialib/cuepoint/{b.id}/move/", {"direction": "up"})
    assert res.status_code == 200
    a.refresh_from_db()
    b.refresh_from_db()
    assert b.seq < a.seq  # b が前に来た


def test_cuepoint_delete(staff_client, db):
    asset = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組X", duration_ms=600_000)
    cue = CueSheet.objects.create(asset=asset)
    p = _content(cue, 1, 60_000)
    res = staff_client.post(f"/medialib/cuepoint/{p.id}/delete/")
    assert res.status_code == 200
    assert not CuePoint.objects.filter(pk=p.id).exists()
