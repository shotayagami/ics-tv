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

500 は Django 既定のハンドラに任せており JSON 形式を保証しない (`default` はその対象外)。
"""

from typing import Any

from ninja import NinjaAPI

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
