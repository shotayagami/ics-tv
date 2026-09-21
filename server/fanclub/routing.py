# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket ルーティング (#27 §3.1 Should)。クリエイター別の会員限定チャット。"""

from django.urls import path

from fanclub.consumers import FcChatConsumer

websocket_urlpatterns = [
    path("ws/fc/<slug:slug>/chat/", FcChatConsumer.as_asgi()),
]
