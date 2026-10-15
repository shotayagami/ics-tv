# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAPI 仕様へエラーレスポンスを一括で出す NinjaAPI。

実行時のエラーは、すべて `{"detail": "<文字列>"}` で返る。SPA は `detail` を文字列として画面に
出すため、形を 1 つに揃えている。

- `HttpError(status, msg)` / 認証失敗: `{"detail": "<文字列>"}` → `ErrorOut`
- 入力検証失敗 (422): `{"detail": "<項目: 理由; ...>", "errors": [{"type", "loc", "msg", ...}]}`
  → `ValidationErrorOut`。ninja 既定の配列形式の `detail` は返さない (文字列を期待する画面が
  配列を描画しようとして落ちるため)。機械可読な詳細は `errors` に載せる

routers は 94 箇所で `HttpError` を投げており、operation ごとに `response={...}` を書くと
153 件の更新が要る。そこで仕様の生成時に次を一括で付ける (operation 側が既に宣言した
ステータスは上書きしない)。

- `default`: 全 operation。宣言外のステータスは `ErrorOut` で返る
- `401`: security を持つ operation
- `422`: 入力 (パラメータ・body) を持つ operation。`ValidationErrorOut` (`errors` は任意)

DB が入力値を受け付けない `DataError` (NUL 文字など) は 400、制約違反の `IntegrityError` は 409 で返す。
ninja に届かない 404 (パスのコンバータ不一致) と 500 は `api.error_views` が JSON で返す。
"""

import logging
from typing import Any

from django.db import DataError, IntegrityError
from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import ValidationError

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
        "detail": {"type": "string", "title": "Detail"},
        # pydantic の検証エラー。HttpError(422, ...) の手書き検証には無い
        "errors": {
            "type": "array",
            "title": "Errors",
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
        },
    },
    "required": ["detail"],
}


_MESSAGES = {
    "missing": "は必須です",
    "string_pattern_mismatch": "を入力してください (空白のみは不可)",
    "string_too_short": "が短すぎます",
    "string_too_long": "が長すぎます",
    "int_parsing": "は整数で指定してください",
    "int_type": "は整数で指定してください",
    "float_parsing": "は数値で指定してください",
    "bool_parsing": "は true / false で指定してください",
    "bool_type": "は true / false で指定してください",
    "string_type": "は文字列で指定してください",
    "greater_than_equal": "が小さすぎます",
    "less_than_equal": "が大きすぎます",
    "literal_error": "の値が不正です",
    "enum": "の値が不正です",
    "json_invalid": "が JSON として不正です",
}


def _summarize(errors: list[dict[str, Any]]) -> str:
    """検証エラーを 1 行にまとめる ("title は必須です; amount は整数で指定してください")。

    loc の先頭 (body / query / path) と、body 引数名 (payload) は読み手に不要なので省く。
    SPA はこの文字列をそのまま画面に出すため、よくある種別は日本語にする。
    """
    parts = []
    for err in errors:
        loc = [str(x) for x in err.get("loc", ())]
        if loc and loc[0] in {"body", "query", "path", "header", "cookie", "form", "file"}:
            loc = loc[1:]
        if loc and loc[0] == "payload":
            loc = loc[1:]
        where = ".".join(loc)
        known = _MESSAGES.get(str(err.get("type")))
        if known:  # "title は必須です" の形 (項目名 + 助詞から始まる定型文)
            parts.append(f"{where} {known}" if where else known.lstrip("はをが"))
        else:
            msg = str(err.get("msg", "invalid"))
            parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts) or "入力値が不正です。"


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

        def integrity_error(
            request: HttpRequest, exc: IntegrityError | type[IntegrityError]
        ) -> HttpResponse:
            """入力が DB の制約 (check / unique / not null) に反したときの 409。

            PositiveIntegerField の下限など、仕様に現れない制約に反する入力は 500 でなく 409 にする。
            内部の不整合が隠れないよう、例外は警告ログに残す。
            """
            logger.warning(
                "IntegrityError on %s: %s",
                request.path,
                exc,
                exc_info=isinstance(exc, BaseException),
            )
            return self.create_response(
                request, {"detail": "データの制約に違反しています。"}, status=409
            )

        def validation_error(
            request: HttpRequest, exc: ValidationError | type[ValidationError]
        ) -> HttpResponse:
            """pydantic の検証エラーを、文字列の `detail` + 詳細の `errors` で返す。"""
            errors = exc.errors if isinstance(exc, ValidationError) else []
            return self.create_response(
                request, {"detail": _summarize(errors), "errors": errors}, status=422
            )

        self.add_exception_handler(ValidationError, validation_error)
        self.add_exception_handler(DataError, data_error)
        self.add_exception_handler(IntegrityError, integrity_error)

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
                        "422", _json(_ref("ValidationErrorOut"), "Validation error")
                    )
                responses.setdefault("default", _json(_ref("ErrorOut"), "Error"))
        return schema
