# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""gRPC server がagentのkeepalive間隔を許容する設定の回帰テスト。"""

from playout.management.commands.grpcserve import SERVER_OPTIONS


def test_server_accepts_agent_keepalive_interval():
    assert dict(SERVER_OPTIONS) == {
        "grpc.http2.min_ping_interval_without_data_ms": 10_000,
        "grpc.http2.max_ping_strikes": 0,
    }
