# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材・CM 専用ダッシュボード (#7)。一覧描画・再正規化・考査・在庫状態算出。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model

from medialib import services
from medialib.models import (
    Asset,
    AssetKind,
    CmCreative,
    CmGrid,
    NormalizeStatus,
    ScreeningStatus,
)


@pytest.fixture
def staff_client(db, client):
    user = get_user_model().objects.create_user("ml_staff", password="x", is_staff=True)
    client.force_login(user)
    return client


@pytest.fixture
def cm(db):
    asset = Asset.objects.create(kind=AssetKind.CM, title="テスト CM", duration_ms=15000)
    return CmCreative.objects.create(asset=asset, advertiser="テスト広告主", grid=CmGrid.G15)


def test_dashboard_renders_assets_and_cm(staff_client, cm):
    body = staff_client.get("/medialib/").content.decode("utf-8")
    assert "素材・CM" in body
    assert "テスト CM" in body
    assert "テスト広告主" in body


def test_dashboard_filters_by_kind(staff_client, db):
    Asset.objects.create(kind=AssetKind.PROGRAM, title="番組素材A", duration_ms=1000)
    Asset.objects.create(kind=AssetKind.FILLER, title="フィラー素材B", duration_ms=1000)
    body = staff_client.get("/medialib/?kind=program").content.decode("utf-8")
    assert "番組素材A" in body
    assert "フィラー素材B" not in body


def test_renormalize_resets_status_and_enqueues(
    staff_client, db, monkeypatch, django_capture_on_commit_callbacks
):
    asset = Asset.objects.create(
        kind=AssetKind.CM,
        title="失敗素材",
        normalize_status=NormalizeStatus.FAILED,
        normalize_error="boom",
    )
    calls = []
    monkeypatch.setattr("medialib.tasks.normalize_asset.delay", lambda pk: calls.append(pk))

    with django_capture_on_commit_callbacks(execute=True):
        res = staff_client.post(f"/medialib/asset/{asset.id}/renormalize/")
    assert res.status_code == 200

    asset.refresh_from_db()
    assert asset.normalize_status == NormalizeStatus.PENDING
    assert asset.normalize_error is None
    assert calls == [asset.id]  # on_commit が走り delay 投入された


def test_screening_approve_and_reject(staff_client, cm):
    res = staff_client.post(f"/medialib/cm/{cm.asset_id}/screening/", {"decision": "approve"})
    assert res.status_code == 200
    cm.refresh_from_db()
    assert cm.screening_status == ScreeningStatus.APPROVED

    staff_client.post(f"/medialib/cm/{cm.asset_id}/screening/", {"decision": "reject"})
    cm.refresh_from_db()
    assert cm.screening_status == ScreeningStatus.REJECTED


def test_screening_rejects_unknown_decision(staff_client, cm):
    res = staff_client.post(f"/medialib/cm/{cm.asset_id}/screening/", {"decision": "bogus"})
    assert res.status_code == 400


def test_dashboard_requires_staff(client, db):
    user = get_user_model().objects.create_user("plain", password="x")
    client.force_login(user)
    res = client.get("/medialib/")
    assert res.status_code in (302, 403)  # staff_member_required は admin login へ誘導


@pytest.mark.django_db
def test_cm_stock_state_transitions():
    asset = Asset.objects.create(kind=AssetKind.CM, title="x")
    cm = CmCreative.objects.create(asset=asset, advertiser="a", grid=CmGrid.G15)
    today = date(2026, 6, 12)

    # 日付 NULL → 要設定
    assert services.cm_stock_state(cm, today)["label"] == "△要設定"

    cm.campaign_start = today - timedelta(days=10)
    cm.campaign_end = today + timedelta(days=10)
    assert services.cm_stock_state(cm, today)["label"] == "●配信中"

    cm.campaign_end = today - timedelta(days=1)
    assert services.cm_stock_state(cm, today)["label"] == "✓終了"

    cm.campaign_start = today + timedelta(days=1)
    cm.campaign_end = today + timedelta(days=10)
    assert services.cm_stock_state(cm, today)["label"] == "○予定"

    # 上限到達
    cm.campaign_start = today - timedelta(days=1)
    cm.max_airings = 10
    cm.aired_count = 10
    assert services.cm_stock_state(cm, today)["label"] == "上限到達"

    # 残少 (残 < 20%)
    cm.aired_count = 9
    assert services.cm_stock_state(cm, today)["label"] == "⚠残少"

    cm.aired_count = 5
    assert services.cm_stock_state(cm, today)["label"] == "●配信中"


# ---- 専用編集画面 (admin 直編集の置換) ----


def test_asset_edit_updates_title(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="旧題", duration_ms=1000)
    res = staff_client.post(f"/medialib/asset/{a.id}/edit/", {"kind": "program", "title": "新題"})
    assert res.status_code == 302
    a.refresh_from_db()
    assert a.title == "新題"


def test_asset_edit_shows_normalized_preview(staff_client, db, monkeypatch):
    # 正規化済み (r2_key あり) なら署名付き <video> プレビューを埋める。
    monkeypatch.setattr(
        "medialib.views.r2.presign_get", lambda key, **k: f"https://r2.example/{key}?sig=x"
    )
    a = Asset.objects.create(
        kind=AssetKind.PROGRAM, title="番組", duration_ms=310000, r2_key="mezzanine/program/3.mp4"
    )
    body = staff_client.get(f"/medialib/asset/{a.id}/edit/").content.decode("utf-8")
    assert "<video" in body
    assert "https://r2.example/mezzanine/program/3.mp4?sig=x" in body


def test_asset_edit_no_preview_without_r2_key(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="未正規化", duration_ms=None)
    body = staff_client.get(f"/medialib/asset/{a.id}/edit/").content.decode("utf-8")
    assert "<video" not in body
    assert "まだありません" in body


def test_asset_preview_url_returns_signed_url(staff_client, db, monkeypatch):
    monkeypatch.setattr(
        "medialib.views.r2.presign_get", lambda key, **k: f"https://r2.example/{key}?sig=y"
    )
    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組", r2_key="mezzanine/program/9.mp4")
    res = staff_client.get(f"/medialib/asset/{a.id}/preview-url/")
    assert res.status_code == 200
    assert res.json()["url"] == "https://r2.example/mezzanine/program/9.mp4?sig=y"


def test_asset_preview_url_404_without_r2_key(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="未正規化")
    res = staff_client.get(f"/medialib/asset/{a.id}/preview-url/")
    assert res.status_code == 404


def test_cm_new_attaches_creative_to_cm_asset(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.CM, title="CM素材", duration_ms=15000)
    res = staff_client.post(
        "/medialib/cm/new/",
        {
            "asset_id": a.id,
            "advertiser": "テスト広告主",
            "grid": "15s",
            "screening_status": "pending",
        },
    )
    assert res.status_code == 302
    assert CmCreative.objects.filter(asset=a, advertiser="テスト広告主").exists()


def test_cm_edit_updates_fields(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.CM, title="CM", duration_ms=15000)
    cm = CmCreative.objects.create(asset=a, advertiser="A", grid=CmGrid.G15)
    res = staff_client.post(
        f"/medialib/cm/{a.id}/edit/",
        {"advertiser": "B社", "grid": "20s", "max_airings": "100", "screening_status": "approved"},
    )
    assert res.status_code == 302
    cm.refresh_from_db()
    assert cm.advertiser == "B社" and cm.grid == "20s" and cm.max_airings == 100


def test_cm_edit_sets_asset_thumbnail(staff_client, db):
    a = Asset.objects.create(kind=AssetKind.CM, title="CM", duration_ms=15000)
    CmCreative.objects.create(asset=a, advertiser="A", grid=CmGrid.G15)
    res = staff_client.post(
        f"/medialib/cm/{a.id}/edit/",
        {
            "advertiser": "A",
            "grid": "15s",
            "screening_status": "pending",
            "thumbnail_url": "https://x/cm.png",
        },
    )
    assert res.status_code == 302
    a.refresh_from_db()
    assert a.thumbnail_url == "https://x/cm.png"


def test_bundle_create_and_add_item(staff_client, db):
    from medialib.models import CmBundle, CmBundleItem

    res = staff_client.post("/medialib/bundle/new/", {"name": "夜間リール"})
    assert res.status_code == 302
    b = CmBundle.objects.get(name="夜間リール")
    a = Asset.objects.create(kind=AssetKind.CM, title="cm", duration_ms=15000)
    cm = CmCreative.objects.create(asset=a, advertiser="A", grid=CmGrid.G15)
    res = staff_client.post(f"/medialib/bundle/{b.id}/items/add/", {"cm_asset_id": cm.asset_id})
    assert res.status_code == 200
    assert CmBundleItem.objects.filter(cm_bundle=b, cm_asset=cm).exists()


def test_filler_create_and_add_item(staff_client, db):
    from medialib.models import FillerItem, FillerPlaylist

    res = staff_client.post("/medialib/filler/new/", {"name": "既定ループ"})
    assert res.status_code == 302
    p = FillerPlaylist.objects.get(name="既定ループ")
    a = Asset.objects.create(kind=AssetKind.FILLER, title="filler1", duration_ms=30000)
    res = staff_client.post(f"/medialib/filler/{p.id}/items/add/", {"asset_id": a.id})
    assert res.status_code == 200
    assert FillerItem.objects.filter(filler_playlist=p, asset=a).exists()


def test_asset_role_flags_default_from_kind(db):
    """新規作成時に役割フラグが kind から導出される (program→番組可 / filler→フィラー可)。"""
    prog = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組")
    assert prog.usable_as_program is True and prog.usable_as_filler is False
    fil = Asset.objects.create(kind=AssetKind.FILLER, title="フィラー")
    assert fil.usable_as_program is False and fil.usable_as_filler is True
    cm = Asset.objects.create(kind=AssetKind.CM, title="CM")
    assert cm.usable_as_program is False and cm.usable_as_filler is False


def test_filler_picker_filters_by_usable_as_filler(staff_client, db):
    """フィラー素材ピッカーは usable_as_filler で絞る: フラグを立てた番組素材も候補に出る。"""
    from medialib.models import FillerPlaylist

    p = FillerPlaylist.objects.create(name="ループ")
    # usable_as_filler を立てていない番組素材はフィラー候補に出ない
    Asset.objects.create(
        kind=AssetKind.PROGRAM, title="ただの番組", normalize_status=NormalizeStatus.READY
    )
    dual = Asset.objects.create(
        kind=AssetKind.PROGRAM, title="フィラー兼用番組", normalize_status=NormalizeStatus.READY
    )
    dual.usable_as_filler = True
    dual.save()

    body = staff_client.get(f"/medialib/filler/{p.id}/edit/").content.decode("utf-8")
    assert "フィラー兼用番組" in body
    assert "ただの番組" not in body


def test_asset_edit_sets_role_flags(staff_client, db):
    """編集画面のチェックボックスで役割フラグを上書きできる (kind は filler のまま番組可に)。"""
    a = Asset.objects.create(kind=AssetKind.FILLER, title="長尺フィラー")
    assert a.usable_as_program is False
    res = staff_client.post(
        f"/medialib/asset/{a.id}/edit/",
        {
            "kind": AssetKind.FILLER,
            "title": "長尺フィラー",
            "thumbnail_url": "",
            "r2_key": "",
            "source_path": "",
            "usable_as_program": "on",
            "usable_as_filler": "on",
        },
    )
    assert res.status_code == 302
    a.refresh_from_db()
    assert a.usable_as_program is True and a.usable_as_filler is True


def test_cuepoint_add_accepts_milliseconds(staff_client, db):
    from medialib.models import CueKind, CueSheet

    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組", duration_ms=305500)
    # 5分5秒500ミリ秒 = 305500ms を ms 入力で表現できる
    res = staff_client.post(
        f"/medialib/asset/{a.id}/cuesheet/add/",
        {"kind": "content", "min": "5", "sec": "5", "ms": "500"},
    )
    assert res.status_code == 200
    cue = CueSheet.objects.get(asset=a)
    assert cue.points.get(kind=CueKind.CONTENT).duration_ms == 305500
