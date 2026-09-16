# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ルート conftest.py — Django setup + 共通 fixture (channel / http_client / grpc_*)。

pytest-django が settings 読み込み (pytest.ini の DJANGO_SETTINGS_MODULE) → sys.path に
icstv_proto を追加 (manage.py と同じ処理)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# manage.py と同じ初期化 (gRPC 生成コードの import path)
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "icstv_proto"))

# Playwright (sync API) は内部で asyncio loop を持つ。pytest-django の test DB setup と
# 同居させると SynchronousOnlyOperation で落ちるため、test 限定で許容する。本番 settings は
# 触らないが、必要なら ALLOWED_HOSTS と同じく env 駆動。
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _celery_eager():
    """テストでは Celery タスクを同期実行 (broker/result-store 非依存)。

    on_commit からの .delay (resolve_channel_now 等) は e2e の実コミットで発火する。eager に
    しないと redis broker/result-store への接続を要求し e2e が落ちる。eager なら同タスクが
    インプロセスで走る (transaction 内の非 e2e テストでは on_commit 自体が発火しないので無影響)。
    """
    from config.celery import app

    prev = (app.conf.task_always_eager, app.conf.task_eager_propagates)
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    yield
    app.conf.task_always_eager, app.conf.task_eager_propagates = prev


@pytest.fixture(autouse=True)
def _inmemory_channel_layer(settings):
    """テストでは Channels の channel layer を InMemory にする (#COMM-01・Redis 非依存)。

    comment_post の WS ブロードキャストや consumer テストが redis へ接続しに行かないように。
    """
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def channel(db):
    """Phase 1 既定 1ch + agent_token。"""
    from core.models import Channel

    return Channel.objects.create(
        name="ICS-TV 1ch",
        slug="ch1",
        enabled=True,
        agent_token="test-agent-token-abc123",
    )


@pytest.fixture
def asset_ready(db):
    """正規化済み Asset (録画番組向け)。"""
    from medialib.models import Asset, AssetKind, NormalizeStatus

    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="ep1",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/ep1.mp4",
        normalize_status=NormalizeStatus.READY,
        width=1920,
        height=1080,
        fps=60.0,
        vcodec="h264",
        acodec="aac",
    )


@pytest.fixture
def http_client():
    from django.test import Client

    return Client()


@pytest.fixture
def staff_user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="staff",
        password="x",  # pragma: allowlist secret - test only
        is_staff=True,
    )


@pytest.fixture
def staff_client(staff_user):
    from django.test import Client

    c = Client()
    c.force_login(staff_user)
    return c


# ---- gRPC fixtures (server が localhost:50051 で動いている前提) ----


@pytest.fixture
def grpc_target():
    import os

    return os.environ.get("ICSTV_GRPC_TARGET", "localhost:50051")


@pytest.fixture
def grpc_channel(grpc_target):
    import grpc

    ch = grpc.insecure_channel(grpc_target)
    yield ch
    ch.close()


@pytest.fixture
def grpc_stub(grpc_channel):
    from icstv.v1 import playout_pb2_grpc

    return playout_pb2_grpc.PlayoutAgentStub(grpc_channel)


@pytest.fixture
def grpc_health_stub(grpc_channel):
    from grpc_health.v1 import health_pb2_grpc

    return health_pb2_grpc.HealthStub(grpc_channel)


@pytest.fixture
def live_agent_token(django_db_blocker):
    """gRPC smoke 用の独立DB接続でモデル既定値・暗号化を適用する。

    gRPC サーバは別プロセスで DATABASE_URL を参照する。pytest の default 接続が
    test DB に切り替わっていても混同しない。隔離した検証DBでのみ実行すること。
    """
    from copy import deepcopy

    import environ
    from django.conf import settings
    from django.db import connections

    from core.models import Channel

    token = "test-agent-token-abc123"
    alias = "grpc_smoke_fixture"
    if alias in connections.databases:
        raise RuntimeError("gRPC fixture connection alias already exists")
    config = deepcopy(settings.DATABASES["default"])
    config.update(environ.Env().db_url("DATABASE_URL"))
    connections.databases[alias] = config
    try:
        with django_db_blocker.unblock():
            Channel.objects.using(alias).update_or_create(
                slug="ch1",
                defaults={"name": "ICS-TV 1ch", "enabled": True, "agent_token": token},
            )
        yield token
    finally:
        connections[alias].close()
        del connections[alias]
        del connections.databases[alias]


@pytest.fixture
def grpc_metadata(live_agent_token):
    """有効な Bearer metadata (実 DB に upsert した agent_token を使う)。"""
    return (("authorization", f"Bearer {live_agent_token}"),)
