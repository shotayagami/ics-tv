# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""server 側 gRPC への薄いクライアント (asyncio)。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from icstv.v1 import playout_pb2, playout_pb2_grpc

# 2026-09-04 の片方向 blackhole では、既存 TCP 接続が約 16 分 half-open のまま残り、
# heartbeat/SubscribeEvents の障害検出が Linux の tcp_retries2 待ちまで遅れた。
# keepalive_timeout_ms は gRPC Core で Linux の TCP_USER_TIMEOUT にも使われる。
RPC_TIMEOUT_SEC = 10
CHANNEL_OPTIONS = (
    ("grpc.keepalive_time_ms", 20_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
)


class ServerClient:
    def __init__(self, target: str, auth_token: str) -> None:
        self._target = target
        self._metadata = (("authorization", f"Bearer {auth_token}"),)
        self._channel: grpc.aio.Channel | None = None
        self._stub: playout_pb2_grpc.PlayoutAgentStub | None = None

    async def connect(self) -> None:
        # WireGuard 越し前提のため平文 (経路で暗号化)。Phase 2 で mTLS 化する余地は残す。
        self._channel = grpc.aio.insecure_channel(self._target, options=CHANNEL_OPTIONS)
        self._stub = playout_pb2_grpc.PlayoutAgentStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()

    async def subscribe_events(
        self, channel_slug: str, last_known_seq: int
    ) -> AsyncIterator[playout_pb2.SubscribeEventsResponse]:
        """SubscribeEventsResponse 全体を yield する (event か control が排他でセットされる)。

        呼び出し側 (main._subscribe_loop) が HasField で event / control を振り分ける。
        control = AgentControl (自動復帰トグル等、#7 O4)。
        """
        assert self._stub is not None
        req = playout_pb2.SubscribeEventsRequest(
            channel_slug=channel_slug,
            last_known_seq=last_known_seq,
        )
        async for resp in self._stub.SubscribeEvents(req, metadata=self._metadata):
            yield resp

    async def report_result(
        self,
        *,
        channel_slug: str,
        idempotency_key: str,
        status: int,
        actual_at: datetime,
        note: str = "",
    ) -> bool:
        assert self._stub is not None
        ts = Timestamp()
        ts.FromDatetime(actual_at)
        req = playout_pb2.ReportResultRequest(
            channel_slug=channel_slug,
            idempotency_key=idempotency_key,
            status=status,
            actual_at=ts,
            note=note,
        )
        resp = await self._stub.ReportResult(
            req,
            metadata=self._metadata,
            timeout=RPC_TIMEOUT_SEC,
        )
        return resp.accepted

    async def heartbeat(
        self,
        *,
        channel_slug: str,
        last_received_seq: int,
        queue_depth: int,
        caspar_health: str,
        slate_active: bool = False,
        feed_state: str = "",
        auto_return: bool = True,
        auto_return_suspended: bool = False,
        layers: list[dict] | None = None,
    ) -> bool:
        assert self._stub is not None
        now = Timestamp()
        now.GetCurrentTime()
        req = playout_pb2.HeartbeatRequest(
            channel_slug=channel_slug,
            now=now,
            last_received_seq=last_received_seq,
            queue_depth=queue_depth,
            caspar_health=caspar_health,
            slate_active=slate_active,
            feed_state=feed_state,
            auto_return=auto_return,
            auto_return_suspended=auto_return_suspended,
            layers=[playout_pb2.LayerState(**ls) for ls in (layers or [])],
        )
        resp = await self._stub.Heartbeat(
            req,
            metadata=self._metadata,
            timeout=RPC_TIMEOUT_SEC,
        )
        return resp.ok

    async def report_interrupt(
        self,
        *,
        channel_slug: str,
        interrupt_key: str,
        kind: int,
        at: datetime,
        detail: str = "",
    ) -> bool:
        """agent 起点の割り込み報告 (feed 断退避/復帰/フラップ停止)。interrupt_key で冪等。"""
        assert self._stub is not None
        ts = Timestamp()
        ts.FromDatetime(at)
        req = playout_pb2.ReportInterruptRequest(
            channel_slug=channel_slug,
            interrupt_key=interrupt_key,
            kind=kind,
            at=ts,
            detail=detail,
        )
        resp = await self._stub.ReportInterrupt(
            req,
            metadata=self._metadata,
            timeout=RPC_TIMEOUT_SEC,
        )
        return resp.accepted

    async def request_recording_upload(
        self, *, channel_slug: str, program_id: int
    ) -> tuple[str, str] | None:
        """生放送録画クリップの presigned PUT URL を要求。戻り値 (upload_url, r2_key)。

        server 側が channel/program を解決できなければ空レスポンス (upload_url="") が返る。
        """
        assert self._stub is not None
        req = playout_pb2.RequestRecordingUploadRequest(
            channel_slug=channel_slug,
            program_id=program_id,
        )
        resp = await self._stub.RequestRecordingUpload(
            req,
            metadata=self._metadata,
            timeout=RPC_TIMEOUT_SEC,
        )
        if not resp.upload_url:
            return None
        return resp.upload_url, resp.r2_key
