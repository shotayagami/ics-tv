# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-6): 素材ライブラリ medialib ダッシュボード admin API。

staff_auth ゲート + 素材一覧/フィルタ + CM 在庫/考査 + バンドル/フィラー概観。再正規化/考査の
操作自体は既存 medialib エンドポイント (tests/api/test_*) の責務。ここは GET の JSON 化を検証。
"""

from __future__ import annotations

from medialib.models import (
    Asset,
    AssetKind,
    CmBundle,
    CmCreative,
    CmGrid,
    NormalizeStatus,
    ScreeningStatus,
)

_DASH = "/api/v1/admin/medialib/dashboard"


def _cm_asset(title="CM素材"):
    return Asset.objects.create(
        kind=AssetKind.CM,
        title=title,
        duration_ms=15000,
        r2_key="cm/x.mp4",
        normalize_status=NormalizeStatus.READY,
    )


def test_dashboard_requires_auth(http_client, db):
    assert http_client.get(_DASH).status_code == 401


def test_dashboard_lists_asset_and_filters(staff_client, asset_ready, db):
    d = staff_client.get(_DASH).json()
    assert any(a["id"] == asset_ready.id for a in d["assets"])
    assert any(k["value"] == "program" for k in d["kinds"])
    assert any(s["value"] == "ready" for s in d["statuses"])
    row = next(a for a in d["assets"] if a["id"] == asset_ready.id)
    assert row["is_program"] is True and row["edit_url"].endswith(f"/asset/{asset_ready.id}/edit/")


def test_dashboard_filter_kind_excludes(staff_client, asset_ready, db):
    d = staff_client.get(_DASH + "?kind=cm").json()  # asset_ready は program → 除外
    assert all(a["id"] != asset_ready.id for a in d["assets"])
    assert d["sel_kind"] == "cm"


def test_dashboard_filter_status(staff_client, asset_ready, db):
    d = staff_client.get(_DASH + "?status=pending").json()  # asset_ready は ready → 除外
    assert all(a["id"] != asset_ready.id for a in d["assets"])
    assert d["sel_status"] == "pending"


def test_dashboard_cm_section(staff_client, db):
    a = _cm_asset()
    CmCreative.objects.create(
        asset=a, advertiser="A社", grid=CmGrid.G15, screening_status=ScreeningStatus.PENDING
    )
    d = staff_client.get(_DASH).json()
    row = next(c for c in d["cms"] if c["asset_id"] == a.id)
    assert row["advertiser"] == "A社" and row["screening_status"] == "pending"
    assert row["aired"].endswith("/∞")  # max_airings 無し
    assert d["n_screening_pending"] >= 1


def test_dashboard_bundles(staff_client, db):
    CmBundle.objects.create(name="バンドルA")
    d = staff_client.get(_DASH).json()
    assert any(b["name"] == "バンドルA" and b["count"] == 0 for b in d["bundles"])
