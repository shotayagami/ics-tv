# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ServerClient のdeadlineとhalf-open検出用channel設定。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from icstv_agent import server_client
from icstv_agent.server_client import CHANNEL_OPTIONS, RPC_TIMEOUT_SEC, ServerClient


class _UnaryCall:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return self.response


class _StreamCall:
    def __init__(self):
        self.calls = []

    def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))

        async def responses():
            if False:
                yield None

        return responses()


class _Stub:
    def __init__(self, channel):
        self.channel = channel
        self.SubscribeEvents = _StreamCall()
        self.ReportResult = _UnaryCall(SimpleNamespace(accepted=True))
        self.Heartbeat = _UnaryCall(SimpleNamespace(ok=True))
        self.ReportInterrupt = _UnaryCall(SimpleNamespace(accepted=True))
        self.RequestRecordingUpload = _UnaryCall(
            SimpleNamespace(upload_url="https://example.invalid/upload", r2_key="recording/test")
        )


def _connected_client(monkeypatch):
    captured = {}
    channel = object()

    def insecure_channel(target, *, options):
        captured.update(target=target, options=options)
        return channel

    monkeypatch.setattr(server_client.grpc.aio, "insecure_channel", insecure_channel)
    monkeypatch.setattr(server_client.playout_pb2_grpc, "PlayoutAgentStub", _Stub)

    client = ServerClient("192.0.2.1:50051", "test-token")
    asyncio.run(client.connect())
    return client, captured


def test_connect_enables_bounded_half_open_detection(monkeypatch):
    _, captured = _connected_client(monkeypatch)

    assert captured == {
        "target": "192.0.2.1:50051",
        "options": CHANNEL_OPTIONS,
    }
    assert dict(CHANNEL_OPTIONS) == {
        "grpc.keepalive_time_ms": 20_000,
        "grpc.keepalive_timeout_ms": 10_000,
        "grpc.keepalive_permit_without_calls": 1,
        "grpc.http2.max_pings_without_data": 0,
    }


def test_unary_rpcs_have_deadline(monkeypatch):
    client, _ = _connected_client(monkeypatch)

    async def exercise():
        await client.report_result(
            channel_slug="ch1",
            idempotency_key="event-1",
            status=1,
            actual_at=datetime.now(UTC),
        )
        await client.heartbeat(
            channel_slug="ch1",
            last_received_seq=1,
            queue_depth=0,
            caspar_health="healthy",
        )
        await client.report_interrupt(
            channel_slug="ch1",
            interrupt_key="interrupt-1",
            kind=1,
            at=datetime.now(UTC),
        )
        await client.request_recording_upload(channel_slug="ch1", program_id=1)

    asyncio.run(exercise())

    stub = client._stub
    assert stub is not None
    for call in (
        stub.ReportResult,
        stub.Heartbeat,
        stub.ReportInterrupt,
        stub.RequestRecordingUpload,
    ):
        assert len(call.calls) == 1
        assert call.calls[0][1]["timeout"] == RPC_TIMEOUT_SEC


def test_subscription_uses_keepalive_without_finite_stream_deadline(monkeypatch):
    client, _ = _connected_client(monkeypatch)

    async def exercise():
        return [response async for response in client.subscribe_events("ch1", 42)]

    assert asyncio.run(exercise()) == []
    stub = client._stub
    assert stub is not None
    assert stub.SubscribeEvents.calls[0][1] == {"metadata": client._metadata}
