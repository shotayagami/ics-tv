# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""納品サブシステム seam (リファクタ Phase 2.1): /internal/delivery-asset + /delivery-refs。

ICS-DELIVERY (別リポ icstv-delivery) が QC+正規化済みの完成 asset を ICS-TV へ登録する内部 API。
X-Internal-Token (DELIVERY_REGISTER_TOKEN) 認証。本編なら Episode.asset 確定 + バックフィル投入。
"""

from __future__ import annotations

import json

import pytest
from django.test import Client, override_settings

TOK = "deliv-tok"  # pragma: allowlist secret - test only
_OVR = override_settings(DELIVERY_REGISTER_TOKEN=TOK)


def _post(payload: dict, token: str = TOK):
    return Client().post(
        "/api/v1/internal/delivery-asset",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"X-Internal-Token": token} if token else {},
    )


@pytest.fixture
def series(db):
    from core.models import Channel
    from scheduling.models import Series

    ch = Channel.objects.create(name="ch1", slug="ch1", enabled=True)
    return Series.objects.create(channel=ch, title="連ドラ")


@_OVR
def test_delivery_asset_requires_token(db):
    assert _post({"r2_key": "k", "title": "t", "duration_ms": 1000}, token="").status_code == 401
    assert (
        _post({"r2_key": "k", "title": "t", "duration_ms": 1000}, token="wrong").status_code == 401
    )


@_OVR
def test_delivery_asset_registers_ready_and_links_episode(series, db):
    from medialib.models import Asset, NormalizeStatus
    from scheduling.models import Episode, EpisodeStatus

    res = _post(
        {
            "r2_key": "mezz/ch1/ep5.mp4",
            "title": "第5話",
            "duration_ms": 1_440_000,
            "width": 1920,
            "height": 1080,
            "series_id": series.id,
            "episode_no": 5,
            "air_date": "2026-07-01",
        }
    )
    assert res.status_code == 200
    body = res.json()
    assert body["backfilled"] is True
    asset = Asset.objects.get(pk=body["asset_id"])
    assert asset.normalize_status == NormalizeStatus.READY  # DELIVERY 側で正規化済み
    assert asset.r2_key == "mezz/ch1/ep5.mp4"
    assert asset.duration_ms == 1_440_000
    ep = Episode.objects.get(pk=body["episode_id"])
    assert ep.asset_id == asset.id
    assert ep.episode_no == 5
    assert ep.status == EpisodeStatus.CONFIRMED


@_OVR
def test_delivery_asset_idempotent_by_r2_key(series, db):
    from medialib.models import Asset

    p = {
        "r2_key": "mezz/x.mp4",
        "title": "t",
        "duration_ms": 1000,
        "series_id": series.id,
        "episode_no": 1,
    }
    a1 = _post(p).json()["asset_id"]
    a2 = _post(p).json()["asset_id"]
    assert a1 == a2
    assert Asset.objects.filter(r2_key="mezz/x.mp4").count() == 1


@_OVR
def test_delivery_asset_resend_with_encode_jitter_is_idempotent(db):
    """crash 後の再正規化再送: duration が encode 揺れ (数 ms) でずれても正当な再送と判定する。"""
    from medialib.models import Asset

    p = {"r2_key": "mezz/jitter.mp4", "title": "再送", "duration_ms": 60_000}
    a1 = _post(p).json()["asset_id"]
    res = _post({**p, "duration_ms": 60_040})
    assert res.status_code == 200
    assert res.json()["asset_id"] == a1
    assert Asset.objects.count() == 1


@_OVR
@pytest.mark.parametrize(
    "clash",
    [
        {"title": "別番組"},  # タイトル違い
        {"duration_ms": 999_000},  # 尺が別物
        {"kind": "cm"},  # 種別違い
    ],
)
def test_delivery_asset_r2_key_collision_rejected_409(db, caplog, clash):
    """同一 r2_key でメタが食い違う登録は別納品物の衝突として 409 + security log (決定#3①)。
    旧 Asset へ無警告で差し替わる (Episode が別素材を掴む) 事故を防ぐ。"""
    from medialib.models import Asset

    base = {"r2_key": "mezz/clash.mp4", "title": "第1話", "duration_ms": 1_440_000}
    a1 = _post(base).json()["asset_id"]

    with caplog.at_level("WARNING", logger="icstv.security"):
        res = _post({**base, **clash})
    assert res.status_code == 409
    assert Asset.objects.count() == 1
    asset = Asset.objects.get(pk=a1)
    assert asset.title == "第1話"  # 既存 Asset は書き換えない
    logged = [json.loads(r.message) for r in caplog.records if r.name == "icstv.security"]
    assert any(
        e["event"] == "internal.delivery_asset" and e["outcome"] == "conflict" for e in logged
    )


@_OVR
def test_delivery_asset_checksum_mismatch_rejected_409(db):
    """checksum が双方にあり食い違えば、他メタが同一でも別物として 409。"""
    base = {"r2_key": "mezz/sum.mp4", "title": "t", "duration_ms": 1000, "checksum": "aaa"}
    assert _post(base).status_code == 200
    assert _post({**base, "checksum": "bbb"}).status_code == 409


@_OVR
def test_delivery_asset_without_episode_no_backfill(db):
    body = _post({"r2_key": "mezz/oneoff.mp4", "title": "単発", "duration_ms": 500}).json()
    assert body["episode_id"] is None
    assert body["backfilled"] is False


@_OVR
def test_delivery_asset_bad_episode_spec(series, db):
    # series_id だけで episode_no/air_date 無し → 400
    res = _post({"r2_key": "k2", "title": "t", "duration_ms": 100, "series_id": series.id})
    assert res.status_code == 400


@_OVR
def test_delivery_refs(series, db):
    res = Client().get("/api/v1/internal/delivery-refs", headers={"X-Internal-Token": TOK})
    assert res.status_code == 200
    body = res.json()
    assert any(c["slug"] == "ch1" for c in body["channels"])
    assert any(s["id"] == series.id for s in body["series"])


def test_delivery_refs_requires_token(db):
    # DELIVERY_REGISTER_TOKEN 未設定 (既定 "") → 常に 401
    assert (
        Client()
        .get("/api/v1/internal/delivery-refs", headers={"X-Internal-Token": "x"})
        .status_code
        == 401
    )
