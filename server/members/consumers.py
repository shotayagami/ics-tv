# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ライブコメントの WebSocket consumer (#COMM-01)。

受信専用 (ブロードキャスト配信)。投稿は従来どおり HTTP(`comment_post`, 会員認証+CSRF)で行い、
作成後に channel layer 経由で同 channel の group へ流す → 接続中の全クライアントへ即 push する。
クライアントからのメッセージは受け付けない (投稿経路を HTTP に一本化し、認証/モデレーションを集約)。
"""

from __future__ import annotations

from channels.generic.websocket import AsyncJsonWebsocketConsumer


def comment_group(slug: str) -> str:
    """チャンネル slug → ブロードキャスト group 名。"""
    return f"comments_{slug}"


class CommentConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.slug = self.scope["url_route"]["kwargs"]["slug"]
        self.group = comment_group(self.slug)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # 投稿は HTTP 経路のみ。WS からの書き込みは無視する。
        return

    async def comment_new(self, event):
        """group_send({"type": "comment.new", "comment": {...}}) を受けてクライアントへ転送。

        ペイロードは構造化済み (api.schemas.CommentItem 同形)。React 島がそのまま描画する
        (旧来の HTML 文字列は廃止 = サーバで HTML を組まない)。
        """
        await self.send_json({"type": "comment.new", "comment": event["comment"]})
