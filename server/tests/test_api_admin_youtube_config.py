# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA: rolling 枠生成テンプレ (YoutubeConfig) の per-channel 閲覧・編集 admin API。

旧 Django admin のみだった YoutubeConfig を SPA 化 (SlotsPage 埋め込み)。GET=未設定なら既定値を
exists=False で提示、POST=upsert。staff_auth ゲート。resync は YouTube 到達のためここでは検証しない。
"""

from __future__ import annotations

import json

from youtube.models import YoutubeConfig

_URL = "/api/v1/admin/youtube/{slug}/config"


def _post(client, slug, payload):
    return client.post(
        _URL.format(slug=slug), data=json.dumps(payload), content_type="application/json"
    )


def test_config_requires_auth(http_client, channel, db):
    assert http_client.get(_URL.format(slug=channel.slug)).status_code == 401


def test_config_get_defaults_when_absent(staff_client, channel, db):
    """未設定チャンネルはモデル既定値を exists=False で返す (DB に行は作らない)。"""
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    assert d["exists"] is False
    assert d["rolling_hours"] == 24 and d["slot_minutes"] == 240
    assert d["privacy"] == "public"
    assert d["privacy_choices"] == ["public", "unlisted", "private"]
    assert not YoutubeConfig.objects.filter(channel=channel).exists()


def test_config_upsert_creates_and_persists(staff_client, channel, db):
    res = _post(
        staff_client,
        channel.slug,
        {
            "title_template": "{channel} 特番 {date}",
            "description_template": "説明 {programs}",
            "privacy": "unlisted",
            "enable_monitor": True,
            "rolling_hours": 12,
            "slot_minutes": 120,
            "nudge_lead_minutes": 5,
            "nudge_template": "次へ {url}",
            "nudge_ended_template": "",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is True and body["rolling_hours"] == 12
    cfg = YoutubeConfig.objects.get(channel=channel)
    assert cfg.title_template == "{channel} 特番 {date}"
    assert cfg.privacy == "unlisted" and cfg.enable_monitor is True
    assert cfg.slot_minutes == 120 and cfg.nudge_lead_minutes == 5

    # 2 回目の POST は同じ行を更新 (get_or_create=upsert)。
    _post(staff_client, channel.slug, {**body, "rolling_hours": 48})
    assert YoutubeConfig.objects.filter(channel=channel).count() == 1
    assert YoutubeConfig.objects.get(channel=channel).rolling_hours == 48


def test_config_rejects_bad_privacy(staff_client, channel, db):
    res = _post(
        staff_client,
        channel.slug,
        {"privacy": "secret", "rolling_hours": 24, "slot_minutes": 240},
    )
    assert res.status_code == 400
    assert not YoutubeConfig.objects.filter(channel=channel).exists()


def test_config_rejects_nonpositive_window(staff_client, channel, db):
    res = _post(
        staff_client,
        channel.slug,
        {"privacy": "public", "rolling_hours": 0, "slot_minutes": 240},
    )
    assert res.status_code == 400
