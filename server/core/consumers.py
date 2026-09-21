# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生放送 timekeeper のリアルタイム push (#25 Phase2)。

受信専用 (members.consumers.CommentConsumer と同じ設計)。クライアントは type だけを
見て既存の GET /api/v1/admin/ops/{slug}/timekeeper ポーリングを即時実行する軽量
invalidate 信号。フルペイロードは送らない (整形ロジックを二重化しないため)。
"""

from __future__ import annotations

import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)


def playout_group(slug: str) -> str:
    """チャンネル slug → ブロードキャスト group 名。"""
    return f"playout_{slug}"


class PlayoutConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # staff 限定 (#25 Phase2 レビュー): この group が運ぶ内容自体は invalidate 信号のみ
        # (D7) で軽微だが、対応する HTTP 側 (op_cm_now 等・GET timekeeper) は全て
        # staff_member_required 済み。CommentConsumer (会員向け公開コンテンツ) と異なりここは
        # 運用者限定情報のため、同じ無認証パターンを流用しない。scope["user"] は
        # config.asgi の AuthMiddlewareStack が解決する。
        user = self.scope.get("user")
        if user is None or not user.is_authenticated or not user.is_staff:
            await self.close(code=4003)
            return
        self.slug = self.scope["url_route"]["kwargs"]["slug"]
        self.group = playout_group(self.slug)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # 受信専用。クライアントからの書き込みは無視する。
        return

    async def playout_update(self, event):
        """group_send({"type": "playout.update"}) を受けてクライアントへ転送する。

        軽量 invalidate 信号のみ (D7)。クライアントは受信したら既存ポーリング関数を
        即時呼ぶだけで、ペイロード整形ロジック (GET応答) を再利用する。
        """
        await self.send_json({"type": "playout.update"})


def broadcast_playout_update(channel_slug: str) -> None:
    """cue発火/スキップ/CM入り等の変化を同chの WS group へ通知する best-effort 呼び出し。

    失敗しても本処理 (POSTのレスポンス等) に影響させない (members._broadcast_new_comment
    と同じ流儀)。
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(playout_group(channel_slug), {"type": "playout.update"})
    except Exception:
        logger.warning("playout broadcast failed", exc_info=True)
