# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開トップ「いま放送中」: ライブ静止画 ingest/配信 + /api/now (#7 ライブ表示)。

R2 は patch でメモリ dict に差し替え (実 R2 へ出ない)。ingest 認証は Channel.agent_token。
host 分離: ingest は管理ホスト (ROOT urlconf) のみ、配信/JSON は公開ホスト。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from core import live_poster
from core.models import Channel
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus

pytestmark = pytest.mark.django_db

PUBLIC = "tv.example.com"
ADMIN = "studio.example.com"
_HOSTS = {
    "ALLOWED_HOSTS": ["testserver", PUBLIC, ADMIN],
    "ICSTV_ADMIN_HOSTS": [ADMIN, "testserver"],
}


@pytest.fixture
def fake_r2(monkeypatch):
    store: dict[str, bytes] = {}

    def put_object(key, body, content_type):
        store[key] = bytes(body)

    def get_object(key):
        if key not in store:
            raise KeyError(key)
        return store[key], "image/jpeg"

    monkeypatch.setattr(live_poster.r2, "put_object", put_object)
    monkeypatch.setattr(live_poster.r2, "get_object", get_object)
    return store


@pytest.fixture
def ch1(db):
    return Channel.objects.create(slug="ch1", name="CH1", enabled=True, agent_token="secret-token")


@override_settings(**_HOSTS)
def test_ingest_stores_and_serves(fake_r2, ch1):
    c = Client()
    # ingest は管理ホスト (ROOT urlconf)。token 一致で 204。
    resp = c.post(
        "/internal/live-poster/ch1/",
        data=b"\xff\xd8jpegbytes",
        content_type="image/jpeg",
        HTTP_X_AGENT_TOKEN="secret-token",
        HTTP_HOST=ADMIN,
    )
    assert resp.status_code == 204
    assert fake_r2[live_poster.r2_key("ch1")] == b"\xff\xd8jpegbytes"

    # 配信は公開ホスト。短期キャッシュ + image/jpeg。
    served = c.get("/live/ch1/poster.jpg", HTTP_HOST=PUBLIC)
    assert served.status_code == 200
    assert served["Content-Type"] == "image/jpeg"
    assert served.content == b"\xff\xd8jpegbytes"
    assert f"max-age={live_poster.SERVE_TTL}" in served["Cache-Control"]


@override_settings(**_HOSTS)
def test_ingest_rejects_bad_token(fake_r2, ch1):
    resp = Client().post(
        "/internal/live-poster/ch1/",
        data=b"x",
        content_type="image/jpeg",
        HTTP_X_AGENT_TOKEN="wrong",
        HTTP_HOST=ADMIN,
    )
    assert resp.status_code == 403
    assert live_poster.r2_key("ch1") not in fake_r2


@override_settings(**_HOSTS)
def test_ingest_not_exposed_on_public_host(fake_r2, ch1):
    # 公開ホストには ingest ルートが存在しない (構造的に露出しない)。
    resp = Client().post(
        "/internal/live-poster/ch1/",
        data=b"x",
        content_type="image/jpeg",
        HTTP_X_AGENT_TOKEN="secret-token",
        HTTP_HOST=PUBLIC,
    )
    assert resp.status_code == 404


@override_settings(**_HOSTS)
def test_serve_404_when_absent(fake_r2, ch1):
    resp = Client().get("/live/ch1/poster.jpg", HTTP_HOST=PUBLIC)
    assert resp.status_code == 404


@override_settings(**_HOSTS)
def test_now_json_live_when_agent_online(fake_r2, ch1):
    now = timezone.now()
    AgentStatus.objects.create(channel=ch1, last_heartbeat_at=now)
    PlayoutEvent.objects.create(
        idempotency_key=uuid.uuid4(),
        channel=ch1,
        scheduled_at=now - timedelta(seconds=30),
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.DONE,
    )
    data = Client().get("/api/now", HTTP_HOST=PUBLIC).json()
    cards = {c["slug"]: c for c in data["cards"]}
    assert cards["ch1"]["live"] is True
    # フィラー中 (番組名なし) でも online なら「放送中」= 準備中にしない。
    assert cards["ch1"]["nowtitle"] == "放送中"
    assert cards["ch1"]["poster"] == "/live/ch1/poster.jpg"


@override_settings(**_HOSTS)
def test_now_json_offline_shows_standby(fake_r2, ch1):
    # heartbeat 無し → offline → 準備中 / live=False。
    data = Client().get("/api/now", HTTP_HOST=PUBLIC).json()
    card = next(c for c in data["cards"] if c["slug"] == "ch1")
    assert card["live"] is False
    assert card["nowtitle"] == "準備中"
