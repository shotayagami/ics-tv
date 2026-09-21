# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""gRPC server for the PlayoutAgent service.

`python manage.py grpcserve --bind 0.0.0.0:50051`

Django とは別プロセスで起動するワークロード (k8s/docker-compose で `command: ...` 切替)。
ORM / settings は Django と共有。
"""

from __future__ import annotations

import asyncio
import logging
import os

import grpc
from django.core.management.base import BaseCommand
from grpc_health.v1 import health_pb2_grpc
from icstv.v1 import playout_pb2_grpc

from playout.grpc_service import PlayoutAgentServicer
from playout.health_service import HealthServicer

logger = logging.getLogger(__name__)

# agent は20秒間隔でkeepalive pingを送る。gRPC server既定の最小受信間隔は5分のため、
# 明示的に許容しないと正常なagentへGOAWAYを返しうる。
SERVER_OPTIONS = (
    ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
    ("grpc.http2.max_ping_strikes", 0),
)


class Command(BaseCommand):
    help = "Run the gRPC server for the agent PlayoutAgent service."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--bind",
            default=os.environ.get("GRPC_BIND", "0.0.0.0:50051"),
            help="bind address (default: 0.0.0.0:50051, env GRPC_BIND)",
        )

    def handle(self, *args, bind: str, **opts) -> None:
        asyncio.run(self._serve(bind))

    async def _serve(self, bind: str) -> None:
        server = grpc.aio.server(options=SERVER_OPTIONS)
        playout_pb2_grpc.add_PlayoutAgentServicer_to_server(PlayoutAgentServicer(), server)
        health_pb2_grpc.add_HealthServicer_to_server(HealthServicer(), server)
        server.add_insecure_port(bind)  # WireGuard 越し前提。Phase 2 で mTLS 余地。
        await server.start()
        self.stdout.write(self.style.SUCCESS(f"gRPC PlayoutAgent listening on {bind}"))
        await server.wait_for_termination()
