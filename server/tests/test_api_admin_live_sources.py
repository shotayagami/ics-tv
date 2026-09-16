# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA: 生入力 LiveSource admin API。

staff_auth ゲート + ingest URL 生成 (host 注入 / passphrase 込み / latency) を検証。
CRUD は Django admin 据え置きのため本 API は GET 閲覧のみ。
"""

from __future__ import annotations

import pytest

from core.models import LiveSource

_URL = "/api/v1/admin/live-sources"


@pytest.fixture
def live_source(db):
    return LiveSource.objects.create(
        name="現場OBS-A",
        rtmp_app="live",
        rtmp_key="obs1",
        srt_latency_ms=2000,
    )


# ---- 認可 ----


def test_requires_auth(http_client, db):
    assert http_client.get(_URL).status_code == 401


# ---- 一覧 + URL 生成 ----


def test_lists_sources_with_ingest_urls(staff_client, live_source, settings):
    settings.ICSTV_INGEST_NODE_HOST = "192.0.2.10"
    d = staff_client.get(_URL).json()
    assert d["ingest_host"] == "192.0.2.10" and d["host_configured"] is True
    assert d["admin_url"] == "/admin/core/livesource/"
    row = next(s for s in d["sources"] if s["name"] == "現場OBS-A")
    assert row["mediamtx_path"] == "live/obs1"
    assert row["srt_latency_ms"] == 2000
    assert row["has_passphrase"] is False
    assert row["srt_url"] == ("srt://192.0.2.10:8890?streamid=publish:live/obs1&latency=2000")
    assert row["rtmp_url"] == "rtmp://192.0.2.10:1935/live/obs1"


def test_passphrase_included_and_flagged(staff_client, settings):
    settings.ICSTV_INGEST_NODE_HOST = "192.0.2.10"
    LiveSource.objects.create(
        name="secured", rtmp_app="live", rtmp_key="sec", srt_passphrase="s3cret"
    )
    row = next(s for s in staff_client.get(_URL).json()["sources"] if s["name"] == "secured")
    assert row["has_passphrase"] is True
    assert "passphrase=s3cret" in row["srt_url"]


def test_host_unconfigured_uses_placeholder(staff_client, live_source, settings):
    settings.ICSTV_INGEST_NODE_HOST = ""
    d = staff_client.get(_URL).json()
    assert d["host_configured"] is False
    assert "<送出ノード private IP>" in d["ingest_host"]
    assert "<送出ノード private IP>" in d["sources"][0]["srt_url"]
