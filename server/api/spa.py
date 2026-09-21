# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""フロント React アプリ (Option B) の SPA シェル配信。

Vite ビルドの index.html をそのまま返す。アセット (JS/CSS) は WhiteNoise が /static/web/ で
配信する (settings.STATICFILES_DIRS [("web", frontend_dist)] / Vite base=/static/web/)。
ビルド未配置時は 503 を返し 500 にしない (CI test / フロント未ビルドのローカルでも安全)。
SPA のクライアントルーティング用に /app/<path> も同じシェルへ流す。
"""

from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def spa_index(request: HttpRequest, rest: str = "") -> HttpResponse:
    index = Path(settings.FRONTEND_DIST) / "index.html"
    if not index.is_file():
        return HttpResponse(
            "frontend not built (cd frontend && npm run build, or build the web image)",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    return HttpResponse(
        index.read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
