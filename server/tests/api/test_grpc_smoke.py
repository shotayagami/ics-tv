# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""gRPC PlayoutAgent サービスの smoke test (要 grpc サーバ起動: docker compose の grpc)。

ICSTV_GRPC_TARGET=localhost:50051 を上書きすれば別ホスト/ポートでも実行可。
これらのテストは Django test DB ではなく、実 DB (docker compose の postgres) に対して動く。
そのため pytest 実行は: docker compose run --rm web pytest -m grpc tests/api/test_grpc_smoke.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.grpc


def test_subscribe_rejects_without_auth(grpc_stub):
    """authorization metadata なしの SubscribeEvents は UNAUTHENTICATED で切られる。"""
    import grpc
    from icstv.v1 import playout_pb2

    req = playout_pb2.SubscribeEventsRequest(channel_slug="ch1", last_known_seq=0)
    with pytest.raises(grpc.RpcError) as exc:
        for _ in grpc_stub.SubscribeEvents(req, timeout=2):
            break
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_subscribe_rejects_wrong_token(grpc_stub):
    """誤った Bearer は UNAUTHENTICATED。"""
    import grpc
    from icstv.v1 import playout_pb2

    req = playout_pb2.SubscribeEventsRequest(channel_slug="ch1", last_known_seq=0)
    md = (("authorization", "Bearer not-a-real-token"),)
    with pytest.raises(grpc.RpcError) as exc:
        for _ in grpc_stub.SubscribeEvents(req, metadata=md, timeout=2):
            break
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_heartbeat_with_valid_token(grpc_stub, grpc_metadata):
    """有効 Bearer + 正しい channel_slug なら Heartbeat は ok=True。"""
    from google.protobuf.timestamp_pb2 import Timestamp
    from icstv.v1 import playout_pb2

    ts = Timestamp()
    ts.GetCurrentTime()
    req = playout_pb2.HeartbeatRequest(
        channel_slug="ch1",
        now=ts,
        last_received_seq=0,
        queue_depth=0,
        caspar_health="unknown",
    )
    resp = grpc_stub.Heartbeat(req, metadata=grpc_metadata, timeout=5)
    assert resp.ok is True


def test_report_result_for_missing_idempotency_key_returns_not_accepted(
    grpc_stub,
    grpc_metadata,
):
    """存在しない idempotency_key に対する ReportResult は accepted=False (404 相当)。"""
    from google.protobuf.timestamp_pb2 import Timestamp
    from icstv.v1 import playout_pb2

    ts = Timestamp()
    ts.GetCurrentTime()
    req = playout_pb2.ReportResultRequest(
        channel_slug="ch1",
        idempotency_key="00000000-0000-0000-0000-000000000000",
        status=playout_pb2.RESULT_STATUS_DONE,
        actual_at=ts,
    )
    resp = grpc_stub.ReportResult(req, metadata=grpc_metadata, timeout=5)
    assert resp.accepted is False


def test_health_check_reports_serving(grpc_health_stub):
    """grpc.health.v1.Health/Check は実 DB クエリを通せば SERVING を返す (readiness/livenessProbe用)。"""
    from grpc_health.v1 import health_pb2

    resp = grpc_health_stub.Check(health_pb2.HealthCheckRequest(), timeout=5)
    assert resp.status == health_pb2.HealthCheckResponse.SERVING
