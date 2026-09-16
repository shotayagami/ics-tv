# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2: 公開番組表グリッド API (構造化 EPG)。"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone


def test_guide_empty(channel):
    d = Client().get("/api/v1/guide").json()
    assert d["base_date"] == d["today"]
    assert d["is_today"] is True
    assert len(d["hours"]) == 24
    col = next(c for c in d["cols"] if c["name"] == channel.name)
    assert col["blocks"] == []
    assert col["slug"] == channel.slug


def test_guide_with_current_block(channel, asset_ready, monkeypatch):
    import datetime as _dt

    from scheduling.models import Program

    # 固定時刻でテスト。timezone.now() を使うと深夜実行時に番組が日跨ぎになり
    # dur_min が day window でクランプされて 60.0 にならない。
    _fixed = _dt.datetime(2030, 1, 15, 12, 0, 0, tzinfo=_dt.UTC)
    monkeypatch.setattr(timezone, "now", lambda: _fixed)
    Program.objects.create(
        channel=channel,
        title="グリッド番組",
        type="recorded",
        asset=asset_ready,
        start_at=_fixed - timedelta(minutes=20),
        end_at=_fixed + timedelta(minutes=40),
        public_visible=True,
    )
    d = Client().get("/api/v1/guide").json()
    blocks = [b for c in d["cols"] for b in c["blocks"]]
    b = next(b for b in blocks if b["title"] == "グリッド番組")
    assert b["is_live"] is True
    assert isinstance(b["top"], (int, float)) and isinstance(b["height"], (int, float))
    assert "style" not in b  # 構造化フィールドのみ (style 文字列は返さない)
    assert d["show_now"] is True
    # client ズーム用の素データ: 生の分オフセット/尺 + 既定密度 + now ライン位置 (分)。
    assert b["dur_min"] == 60.0  # 20分前開始〜40分後終了 = 60分
    assert isinstance(b["start_min"], (int, float))
    assert d["px_per_min"] == 2.2  # 既定密度 (core.epg.PX_PER_MIN)
    assert isinstance(d["now_min"], (int, float))


def test_guide_date_param(channel):
    d = Client().get("/api/v1/guide?date=2030-01-15").json()
    assert d["base_date"] == "2030-01-15"
    assert d["is_today"] is False
    assert d["prev_date"] == "2030-01-14" and d["next_date"] == "2030-01-16"
    assert d["show_now"] is False  # 未来日は now ライン無し
