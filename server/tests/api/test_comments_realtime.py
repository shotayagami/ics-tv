# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ライブコメントのリアルタイム配信 (#COMM-01)。

WebSocket consumer のリレーと、HTTP 投稿(comment_post)→ channel layer ブロードキャストを
検証する。channel layer は conftest の autouse fixture で InMemory に差し替え済み (Redis 非依存)。
async は async_to_sync でラップしてプレーンな pytest から駆動する (pytest-asyncio 不要)。
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import override_settings
from django.utils import timezone

from members.consumers import comment_group
from members.models import Member
from members.routing import websocket_urlpatterns

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def test_consumer_relays_group_message(db):
    """consumer が接続→group参加し、group_send された comment.new をクライアントへ転送する。"""

    async def scenario():
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), "/ws/comments/ch1/")
        connected, _ = await comm.connect()
        assert connected
        layer = get_channel_layer()
        # #Phase1: ペイロードは構造化済み (api.schemas.CommentItem 同形)。html 文字列は廃止。
        await layer.group_send(
            comment_group("ch1"),
            {"type": "comment.new", "comment": {"id": 7, "body": "hi", "nickname": "n"}},
        )
        msg = await comm.receive_json_from()
        assert msg["type"] == "comment.new"
        assert msg["comment"]["id"] == 7 and "hi" in msg["comment"]["body"]
        await comm.disconnect()

    async_to_sync(scenario)()


@_PUBLIC_HOST
def test_comment_post_broadcasts(http_client, channel, db):
    """HTTP 投稿が同 channel の group へ comment.new を配信する (end-to-end)。"""
    _member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(comment_group("ch1"), "probe")
    res = http_client.post(
        "/members/comments/ch1/post/",
        {"body": "やっほー"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert res.status_code == 200
    msg = async_to_sync(layer.receive)("probe")
    assert msg["type"] == "comment.new"
    assert "やっほー" in msg["comment"]["body"] and msg["comment"]["id"]


@_PUBLIC_HOST
def test_channel_page_mounts_player_island(http_client, channel, db):
    """視聴ページは React 島 (#Phase1) をマウントする。WS 接続は player.js (バンドル) 内。"""
    body = http_client.get("/ch/ch1/").content.decode("utf-8")
    assert 'id="player-island"' in body
    assert "/static/web/player/player.js" in body
