# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAPI 仕様のエラーレスポンス宣言が、実行時の応答形と食い違わないことを守る。

宣言は `api.openapi_errors.SpecNinjaAPI` が一括で付ける。ここでは (1) 全 operation に
`default` が付くこと、(2) 実際に返る 401 / 422 の body が宣言したスキーマに適合することを見る。
"""

import jsonschema
import pytest

from api.management.commands.dump_openapi import _merged_schema

_METHODS = ("get", "put", "post", "delete", "patch")


@pytest.fixture(scope="module")
def spec():
    return _merged_schema()


def _inline(spec, node):
    """$ref を components から再帰展開する (jsonschema 用の最小実装)。"""
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _inline(spec, spec["components"]["schemas"][name])
        return {k: _inline(spec, v) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(spec, v) for v in node]
    return node


def test_every_operation_declares_default_error(spec):
    missing = [
        f"{m.upper()} {path}"
        for path, item in spec["paths"].items()
        for m in _METHODS
        if m in item and "default" not in item[m]["responses"]
    ]
    assert not missing, missing
    assert {"ErrorOut", "ValidationErrorOut"} <= set(spec["components"]["schemas"])


def test_response_status_keys_are_strings(spec):
    # int と str の混在は dump_openapi の sort_keys を落とす
    for item in spec["paths"].values():
        for m in _METHODS:
            if m in item:
                assert all(isinstance(k, str) for k in item[m]["responses"])


def _first_staff_get(spec):
    """staff 認証で、パラメータを持たない最初の GET operation (path, operation)。"""
    for path, item in sorted(spec["paths"].items()):
        op = item.get("get")
        if op and not op.get("parameters") and {"SessionAuthIsStaff": []} in op.get("security", []):
            return path, op
    raise AssertionError("staff 認証の GET operation が仕様に無い")


def test_unauthenticated_staff_call_matches_declared_401(client, spec):
    path, op = _first_staff_get(spec)
    resp = client.get(path)
    assert resp.status_code == 401
    schema = _inline(spec, op["responses"]["401"]["content"]["application/json"]["schema"])
    jsonschema.validate(resp.json(), schema)


def test_validation_error_matches_declared_422(client, spec):
    resp = client.post("/api/v1/auth/login", data={}, content_type="application/json")
    assert resp.status_code == 422
    declared = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]["422"]
    schema = _inline(spec, declared["content"]["application/json"]["schema"])
    jsonschema.validate(resp.json(), schema)
