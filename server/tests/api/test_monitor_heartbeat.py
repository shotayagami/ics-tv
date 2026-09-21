# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""クラスタ外 (Zabbix) からの死活監視の読み出し口: /internal/monitor/heartbeat。

X-Internal-Token (MONITOR_READ_TOKEN) 認証・読み取り専用。判定は playout.tasks.check_agent_liveness と
同じ閾値 (OFFLINE_THRESHOLD_SEC) と on_air の定義を使う (二重の真実を作らない)。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from playout.grpc_service import persist_heartbeat
from playout.models import AgentStatus

TOK = "mon-tok"  # pragma: allowlist secret - test only
_OVR = override_settings(MONITOR_READ_TOKEN=TOK)
PATH = "/api/v1/internal/monitor/heartbeat"


def _get(token: str = TOK):
    return Client().get(PATH, headers={"X-Internal-Token": token} if token else {})


def _beat(channel):
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)


@_OVR
def test_requires_token(db):
    assert _get(token="").status_code == 401
    assert _get(token="wrong").status_code == 401


@_OVR
def test_token_unset_is_401(db):
    # トークン未設定 (空) なら常に 401 (= 読み出し口は無効)。
    with override_settings(MONITOR_READ_TOKEN=""):
        assert _get().status_code == 401


@override_settings(
    MONITOR_READ_TOKEN=TOK, ICSTV_ADMIN_HOSTS=[], ICSTV_OPS_HOSTS=[], ICSTV_DELIVERY_HOSTS=[]
)
def test_404_on_public_host(db):
    # 公開ホスト tv.* には internal ルータ自体が無い (#sec M-4)。監視の読み出し口も例外にしない。
    assert _get().status_code == 404


@_OVR
def test_fresh_heartbeat_is_online(channel):
    _beat(channel)
    r = _get()
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["threshold_sec"] == 90
    (c,) = body["channels"]
    assert c["slug"] == "ch1" and c["name"] == channel.name
    assert c["offline"] is False
    assert 0 <= c["seconds_since_heartbeat"] < 5
    assert c["on_air"] is True  # fixture は broadcast_windows 未設定 = 常時 on-air


@_OVR
def test_stale_heartbeat_is_offline(channel):
    _beat(channel)
    AgentStatus.objects.filter(channel=channel).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=5)
    )
    (c,) = _get().json()["channels"]
    assert c["offline"] is True
    assert c["seconds_since_heartbeat"] >= 300


@_OVR
def test_no_agent_status_reports_offline_with_null_age(channel):
    # 一度も heartbeat が来ていない channel。行が無いことを「不明」で隠さず offline として出す。
    (c,) = _get().json()["channels"]
    assert c["seconds_since_heartbeat"] is None
    assert c["offline"] is True


@_OVR
def test_disabled_channel_is_excluded(channel):
    # 退役 (enabled=False) は対象外。check_agent_liveness と同じ規約 (ch2 の 74 日 latch の再発防止)。
    _beat(channel)
    channel.enabled = False
    channel.save(update_fields=["enabled"])
    assert _get().json()["channels"] == []


@_OVR
def test_off_air_is_reported(channel):
    # 窓を今から 6 時間後の 1 分幅にして必ず休止中にする (test_agent_ops と同じ手口)。
    now_jst = timezone.localtime(timezone.now())
    start = (now_jst + timedelta(hours=6)).strftime("%H:%M")
    end = (now_jst + timedelta(hours=6, minutes=1)).strftime("%H:%M")
    channel.broadcast_windows = [{"start": start, "end": end}]
    channel.save(update_fields=["broadcast_windows"])
    _beat(channel)
    (c,) = _get().json()["channels"]
    assert c["on_air"] is False
    assert c["offline"] is False
