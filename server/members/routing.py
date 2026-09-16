# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket ルーティング (#COMM-01)。チャンネル別ライブコメント。"""

from django.urls import path

from members.consumers import CommentConsumer

websocket_urlpatterns = [
    path("ws/comments/<slug:slug>/", CommentConsumer.as_asgi()),
]
