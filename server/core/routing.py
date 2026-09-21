# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebSocket ルーティング (#25 Phase2)。生放送 timekeeper のリアルタイム push。"""

from django.urls import path

from core.consumers import PlayoutConsumer

websocket_urlpatterns = [
    path("ws/playout/<slug:slug>/", PlayoutConsumer.as_asgi()),
]
