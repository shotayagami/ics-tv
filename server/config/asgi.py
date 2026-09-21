# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ASGI config (#COMM-01 ライブコメント / #25 Phase2 timekeeper push)。

HTTP は通常の Django、WebSocket は members(ライブコメント) + core(timekeeper push) の
routing へ振り分ける。gunicorn の uvicorn worker でこの application を配信する。
get_asgi_application() を先に呼んで Django をセットアップしてから、models を触る
consumer を import する (Channels 定石)。
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from core.routing import websocket_urlpatterns as core_ws_patterns  # noqa: E402
from fanclub.routing import websocket_urlpatterns as fanclub_ws_patterns  # noqa: E402
from members.routing import websocket_urlpatterns as members_ws_patterns  # noqa: E402

websocket_urlpatterns = members_ws_patterns + fanclub_ws_patterns + core_ws_patterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # 同一オリジン (公開ホスト/管理ホスト) からの WS のみ許可。投稿/操作は HTTP 側で
        # 会員認証/CSRF or staff_member_required。
        # AuthMiddlewareStack: セッション Cookie から scope["user"] を解決する (Channels 定石)。
        # CommentConsumer は元々 scope["user"] を見ないため既存挙動に影響しない。
        # core.consumers.PlayoutConsumer は staff 限定情報 (#25 Phase2) を扱うため、これで
        # scope["user"] を解決し、consumer 側で is_staff を検査できるようにする。
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)
