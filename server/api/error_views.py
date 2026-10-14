# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`/api/` 配下の 404 / 500 を JSON (`ErrorOut` と同形) で返す Django のエラーハンドラ。

ninja が処理する例外は `{"detail": ...}` を返すが、次の 2 つは ninja に届かず Django 既定の
HTML になる。OpenAPI の `default` (ErrorOut) と一致させるため、API パスだけ JSON にする。

- 404: パスのコンバータ (`{int:...}`) に合わない値 (URL ルーターが先に不一致と判断する)
- 500: ninja が処理しない例外。Django は handler500 を呼ぶ前に例外ログとエラー通知
  (got_request_exception) を済ませるため、通知は従来どおり届く

API 以外のパスは Django 既定のハンドラへ委譲する。各 urlconf で
`handler404` / `handler500` に指定する。
"""

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import defaults

_API_PREFIX = "/api/"


def not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    if request.path.startswith(_API_PREFIX):
        return JsonResponse({"detail": "Not Found"}, status=404)
    return defaults.page_not_found(request, exception)


def server_error(request: HttpRequest) -> HttpResponse:
    if request.path.startswith(_API_PREFIX):
        return JsonResponse({"detail": "Internal Server Error"}, status=500)
    return defaults.server_error(request)
