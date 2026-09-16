# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#22 内部連携 /api/v1/internal/weather-import の単体テスト。

トークン無/誤=401、正=Asset 作成 (source_path=r2://、kind 既定 program) を検証する。
"""

from __future__ import annotations

import json

import pytest
from django.test import Client, override_settings

from medialib.models import Asset, AssetKind

TOKEN = "test-internal-token-xyz"
URL = "/api/v1/internal/weather-import"


def _post(body: dict, token: str | None = None) -> object:
    headers = {"X-Internal-Token": token} if token else {}
    return Client().post(
        URL, data=json.dumps(body), content_type="application/json", headers=headers
    )


@override_settings(WEATHER_IMPORT_TOKEN=TOKEN)
@pytest.mark.django_db
def test_import_requires_token():
    assert _post({"r2_key": "ingest/weather/1/2026-06-24T0857.mp4"}).status_code == 401
    assert _post({"r2_key": "x"}, token="wrong").status_code == 401


@override_settings(WEATHER_IMPORT_TOKEN="")
@pytest.mark.django_db
def test_import_disabled_when_token_unset():
    # トークン未設定なら正しいヘッダでも無効 (401)
    assert _post({"r2_key": "x"}, token="anything").status_code == 401


@override_settings(WEATHER_IMPORT_TOKEN=TOKEN)
@pytest.mark.django_db
def test_import_creates_asset():
    key = "ingest/weather/1/2026-06-24T0857.mp4"
    res = _post({"r2_key": key, "title": "天気予報 流用素材"}, token=TOKEN)
    assert res.status_code == 200, res.content
    asset_id = res.json()["asset_id"]
    asset = Asset.objects.get(pk=asset_id)
    assert asset.kind == AssetKind.PROGRAM
    assert asset.source_path == f"r2://{key}"
    assert asset.title == "天気予報 流用素材"


@override_settings(WEATHER_IMPORT_TOKEN=TOKEN)
@pytest.mark.django_db
def test_import_kind_filler():
    res = _post({"r2_key": "ingest/weather/1/x.mp4", "kind": "filler"}, token=TOKEN)
    assert res.status_code == 200
    assert Asset.objects.get(pk=res.json()["asset_id"]).kind == AssetKind.FILLER
