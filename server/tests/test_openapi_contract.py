# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-free schema contract checks; runtime authorization remains separate."""

import json

from django.core.management import call_command

from api.management.commands.dump_openapi import _merged_schema

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def test_merged_operation_ids_and_security_references():
    schema = _merged_schema()
    schemes = schema["components"]["securitySchemes"]
    ids = []
    requirements = list(schema.get("security", []))
    for item in schema["paths"].values():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            ids.append(operation["operationId"])
            requirements.extend(operation.get("security", []))
    assert ids and len(ids) == len(set(ids))
    assert {"MemberAuth", "MemberTokenAuth"} <= schemes.keys()
    assert all(name in schemes for requirement in requirements for name in requirement)


def test_dump_matches_merged_schema(tmp_path):
    target = tmp_path / "openapi.json"
    call_command("dump_openapi", out=str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(
        json.dumps(_merged_schema())
    )


def test_public_urlconf_does_not_mount_admin_or_internal_api():
    import pytest
    from django.urls import Resolver404, resolve

    for path in ("/api/v1/admin/ops/ch1/status", "/api/v1/internal/weather-import"):
        assert resolve(path, urlconf="config.urls")
        with pytest.raises(Resolver404):
            resolve(path, urlconf="config.urls_public")
    assert resolve("/api/v1/health/", urlconf="config.urls_public")
