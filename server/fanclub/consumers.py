# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員限定チャットの WebSocket consumer (#27 §3.1 Should)。

members.consumers.CommentConsumer (#COMM-01) と同じ「受信専用ブロードキャスト」方式だが、
あちらは公開コメントなので接続を認証しないのに対し、**こちらは接続時にゲートを検査する**
(資格の無い接続に新着発言を垂れ流さない)。投稿は HTTP (fanclub.views.chat_post、
会員認証+CSRF) のみで、WS からの書き込みは受け付けない。
"""

from __future__ import annotations

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


def chat_group(slug: str) -> str:
    """creator slug → ブロードキャスト group 名。"""
    return f"fcchat_{slug}"


@database_sync_to_async
def _can_join(session, slug: str) -> bool:
    """セッションの会員が creator のチャット資格を満たすか (同期 ORM を async から呼ぶ)。"""
    from fanclub import services
    from fanclub.models import Creator, CreatorStatus
    from members.auth import SESSION_KEY
    from members.models import Member

    member_id = session.get(SESSION_KEY)
    if not member_id:
        return False
    member = Member.objects.filter(pk=member_id).first()
    creator = Creator.objects.filter(slug=slug, status=CreatorStatus.ACTIVE).first()
    return member is not None and services.can_use_chat(member, creator)


class FcChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.slug = self.scope["url_route"]["kwargs"]["slug"]
        if not await _can_join(self.scope["session"], self.slug):
            await self.close(code=4403)  # 資格なし (メッセージを流す前に切る)
            return
        self.group = chat_group(self.slug)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # 投稿は HTTP 経路のみ。WS からの書き込みは無視する (#COMM-01 と同じ規律)。
        return

    async def chat_new(self, event):
        """group_send({"type": "chat.new", "message": {...}}) を受けてクライアントへ転送。"""
        await self.send_json({"type": "chat.new", "message": event["message"]})
