# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP アクセスログ記録 (awstats 的な集計の入力)。ホスト別に正確に記録するのが目的
(ingress-nginx の既定ログには Host が乗らず tv.*/studio.*/ops.* を区別できないため、実運用で判明)。

集計そのものは analytics.stats 側 (読み取り時に GROUP BY)。記録の失敗が本編のレスポンスに
影響しないよう、DB 書き込みは例外を握りつぶす (テレメトリはベストエフォート)。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 静的配信 (WhiteNoise) はアクセス数の分析価値が無く量だけ多いため除外。
_EXCLUDE_PREFIXES = ("/static/",)

# 「ページ」相当 (API/WS/内部連携ではない、人が開くURL) とみなす path prefix の除外リスト。
_NON_PAGE_PREFIXES = ("/api/", "/ws/", "/internal/", "/admin-ui/", "/_studio_")


def _is_page(path: str) -> bool:
    return not path.startswith(_NON_PAGE_PREFIXES)


class AccessLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if not path.startswith(_EXCLUDE_PREFIXES):
            self._record(request, response, path)
        return response

    def _record(self, request, response, path: str) -> None:
        from analytics.models import AccessLogEntry

        try:
            AccessLogEntry.objects.create(
                host=request.get_host().split(":")[0].lower(),
                method=request.method,
                path=path[:512],
                status=response.status_code,
                is_page=_is_page(path),
            )
        except Exception:
            logger.warning("access log record failed", exc_info=True)
