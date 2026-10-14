# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAPI 仕様へエラーレスポンスを一括で出す NinjaAPI。

実行時のエラーは次の 2 形式で返る (ninja 既定のハンドラ。本モジュールは形を変えない)。

- `HttpError(status, msg)` / 認証失敗: `{"detail": "<文字列>"}` → `ErrorOut`
- 入力検証失敗 (422): `{"detail": [{"type", "loc", "msg", ...}]}` → `ValidationErrorOut`

routers は 94 箇所で `HttpError` を投げており、operation ごとに `response={...}` を書くと
153 件の更新が要る。そこで仕様の生成時に次を一括で付ける (operation 側が既に宣言した
ステータスは上書きしない)。

- `default`: 全 operation。宣言外のステータスは `ErrorOut` で返る
- `401`: security を持つ operation
- `422`: 入力 (パラメータ・body) を持つ operation。`HttpError(422, ...)` も同じ 422 を使うため
  `ValidationErrorOut` と `ErrorOut` の anyOf にする

DB が入力値を受け付けない `DataError` (NUL 文字など) は 400 で返す。
ninja に届かない 404 (パスのコンバータ不一致) と 500 は `api.error_views` が JSON で返す。
"""

import logging
from typing import Any

from django.db import DataError
from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI

logger = logging.getLogger(__name__)

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_ERROR_OUT = {
    "type": "object",
    "title": "ErrorOut",
    "properties": {"detail": {"type": "string", "title": "Detail"}},
    "required": ["detail"],
}

_VALIDATION_ERROR_OUT = {
    "type": "object",
    "title": "ValidationErrorOut",
    "properties": {
        "detail": {
            "type": "array",
            "title": "Detail",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "loc": {
                        "type": "array",
                        "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                    },
                    "msg": {"type": "string"},
                },
                "required": ["type", "loc", "msg"],
                "additionalProperties": True,
            },
        }
    },
    "required": ["detail"],
}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


class SpecNinjaAPI(NinjaAPI):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        def data_error(request: HttpRequest, exc: DataError | type[DataError]) -> HttpResponse:
            """DB が入力値を受け付けなかった (NUL 文字・範囲外の数値など) ときの 400。

            入力検証をすり抜けた値が原因なので 500 でなく 400 にする。内部のデータ不整合が
            隠れないよう、例外は警告ログに残す。
            """
            logger.warning(
                "DataError on %s: %s", request.path, exc, exc_info=isinstance(exc, BaseException)
            )
            return self.create_response(request, {"detail": "入力値が不正です。"}, status=400)

        self.add_exception_handler(DataError, data_error)

    def get_openapi_schema(self, **kwargs: Any) -> Any:
        schema = super().get_openapi_schema(**kwargs)
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas.setdefault("ErrorOut", _ERROR_OUT)
        schemas.setdefault("ValidationErrorOut", _VALIDATION_ERROR_OUT)
        for item in schema.get("paths", {}).values():
            for method in _HTTP_METHODS:
                op = item.get(method)
                if op is None:
                    continue
                # ninja のステータスキーは int。"default" (str) と混在すると sort_keys の JSON
                # 出力が落ちるため、str に揃えてから足す。
                responses = {str(k): v for k, v in op.get("responses", {}).items()}
                op["responses"] = responses
                if op.get("security"):
                    responses.setdefault("401", _json(_ref("ErrorOut"), "Unauthorized"))
                if op.get("parameters") or op.get("requestBody"):
                    responses.setdefault(
                        "422",
                        _json(
                            {"anyOf": [_ref("ValidationErrorOut"), _ref("ErrorOut")]},
                            "Validation error",
                        ),
                    )
                responses.setdefault("default", _json(_ref("ErrorOut"), "Error"))
        return schema
