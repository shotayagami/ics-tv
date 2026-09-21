# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""gRPC 標準ヘルスチェック (grpc.health.v1.Health)。

grpcserve は Django の request/response サイクルを経ないため、request_started/finished
シグナルで走る close_old_connections() が一度も発火しない。DB 接続が一度死ぬと
(ネットワーク瞬断・pg 再起動等)、プロセスが生きている限り誰も気付かず全 RPC が
`OperationalError: the connection is closed` で落ち続ける
(2026-07-27, icstv-grpc が5.5時間気付かれず放送スケジュール停止)。

Check() は実クエリで確認し、失敗時は connection.close() で明示的に破棄する。sync_to_async
は thread_sensitive=True (既定) のため grpc_service.py の他ハンドラと同じスレッド=同じ
Django connection を共有しており、ここで close() すれば次のクエリで自動再接続される
(readiness/livenessProbe を通した定期呼び出しが、そのまま自己修復のトリガーになる)。
"""

from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.db import connection
from grpc_health.v1 import health_pb2, health_pb2_grpc

logger = logging.getLogger(__name__)


def _check_db() -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        logger.exception("health check: DB 接続確認に失敗、接続を破棄し次回クエリで再接続させる")
        connection.close()
        return False


class HealthServicer(health_pb2_grpc.HealthServicer):
    async def Check(self, request, context):
        ok = await sync_to_async(_check_db)()
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if ok
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        return health_pb2.HealthCheckResponse(status=status)
