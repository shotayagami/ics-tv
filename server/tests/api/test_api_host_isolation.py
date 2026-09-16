# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-4: 内部連携 (M2M) + 管理 API を公開ホスト tv.* から構造的に非露出にする。

public_api (視聴者島) と internal_admin_api (内部連携 + 管理 SPA) を別 NinjaAPI に分割し、
公開 urlconf には public_api だけをマウントする。公開ホストでは internal/admin ルート自体が
存在しない (=404)。管理ホスト (testserver) では従来どおり到達でき、認証層で 401 になる。
"""

from __future__ import annotations

from django.test import override_settings

# 公開ホストへ倒す (testserver を admin/ops/delivery いずれの許可ホストからも外す)。
_PUBLIC = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_OPS_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


@_PUBLIC
def test_internal_endpoint_404_on_public_host(http_client, db):
    res = http_client.post(
        "/api/v1/internal/weather-import",
        data="{}",
        content_type="application/json",
        HTTP_X_INTERNAL_TOKEN="whatever",
    )
    assert res.status_code == 404  # ルータ自体が公開ホストに存在しない (401 ですらない)


@_PUBLIC
def test_admin_endpoint_404_on_public_host(http_client, db):
    res = http_client.get("/api/v1/admin/ops/ch1/status")
    assert res.status_code == 404


@_PUBLIC
def test_public_island_still_served_on_public_host(http_client, db):
    res = http_client.get("/api/v1/health/")
    assert res.status_code == 200


def test_internal_endpoint_resolves_on_admin_host(http_client, db):
    # 管理ホスト (testserver) では internal ルータが存在 → トークン無しは 401 (404 ではない)。
    res = http_client.post(
        "/api/v1/internal/weather-import",
        data="{}",
        content_type="application/json",
    )
    assert res.status_code == 401


def test_public_island_still_served_on_admin_host(http_client, db):
    # 管理ホストでも公開島 API はバックトラックで解決される (両 API 同一 prefix マウント)。
    res = http_client.get("/api/v1/health/")
    assert res.status_code == 200
