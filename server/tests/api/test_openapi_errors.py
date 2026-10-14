# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAPI 仕様のエラーレスポンス宣言が、実行時の応答形と食い違わないことを守る。

宣言は `api.openapi_errors.SpecNinjaAPI` が一括で付ける。ここでは (1) 全 operation に
`default` が付くこと、(2) 実際に返る 401 / 422 の body が宣言したスキーマに適合することを見る。
"""

import json

import jsonschema
import pytest

from api.management.commands.dump_openapi import _merged_schema


def _current_schema() -> dict:
    """マージ済み仕様。別テストが残したホスト別 urlconf (公開ホスト) の影響を受けないよう、
    既定の urlconf に戻してから生成する (残っていると ninja の名前空間を逆引きできない)。"""
    from django.urls import set_urlconf

    set_urlconf(None)
    return _merged_schema()


def _skip_unless_route(path: str) -> None:
    """配布版には無いルート (例: 天気取り込み、調達) のテストをスキップする。"""
    if path not in _current_schema()["paths"]:
        pytest.skip(f"{path} はこのツリーに無い")


_METHODS = ("get", "put", "post", "delete", "patch")


@pytest.fixture(scope="module")
def spec():
    return _current_schema()


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


def test_api_not_found_is_json(client):
    # `{int:...}` に合わない値は ninja に届かず URL ルーターが 404 にする
    resp = client.get("/api/v1/program/abc")
    assert resp.status_code == 404
    assert resp["Content-Type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


def test_non_api_not_found_stays_html(client):
    resp = client.get("/no-such-page/")
    assert resp.status_code == 404
    assert resp["Content-Type"].startswith("text/html")


def test_server_error_handler_is_json_only_under_api(rf):
    from api.error_views import server_error

    api = server_error(rf.get("/api/v1/health/"))
    assert api.status_code == 500
    assert api["Content-Type"].startswith("application/json")
    assert server_error(rf.get("/studio/")).status_code == 500
    assert server_error(rf.get("/studio/"))["Content-Type"].startswith("text/html")


def test_urlconfs_wire_json_handlers():
    import importlib
    import importlib.util

    checked = 0
    for name in ("urls", "urls_ops", "urls_public", "urls_tenant_studio"):
        if importlib.util.find_spec(f"config.{name}") is None:  # 配布版に無い urlconf
            continue
        mod = importlib.import_module(f"config.{name}")
        assert mod.handler404 == "api.error_views.not_found", name
        assert mod.handler500 == "api.error_views.server_error", name
        checked += 1
    assert checked >= 3


def test_nul_in_query_is_400_not_500(db, client):
    # PostgreSQL の text は NUL を保持できず DataError になる (schemathesis が検出)
    resp = client.get("/api/v1/search?q=a%00b")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "入力値が不正です。"}


def test_sales_allocation_invalid_date_is_422_not_500(staff_client, channel):
    # strptime の ValueError が 500 になっていた (schemathesis が検出)
    for bad in ("not-a-date", "2026-13-45"):
        resp = staff_client.get("/api/v1/admin/sales/allocation", {"date": bad})
        assert resp.status_code == 422, bad


def test_close_month_out_of_range_is_422_not_500(staff_client):
    # calendar.IllegalMonthError (ValueError) が 500 になっていた (schemathesis が検出)
    resp = staff_client.post(
        "/api/v1/admin/billing/close-month",
        {"year": 2026, "month": 0, "force": False},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_weather_reconcile_missing_series_is_409_not_500(db, client, settings):
    _skip_unless_route("/api/v1/internal/weather-reconcile")
    settings.WEATHER_IMPORT_TOKEN = "t"  # pragma: allowlist secret - test only
    resp = client.post(
        "/api/v1/internal/weather-reconcile",
        {"channel_id": 999999},
        content_type="application/json",
        HTTP_X_INTERNAL_TOKEN="t",
    )
    assert resp.status_code == 409


def test_creator_contract_invalid_date_is_422_not_500(staff_client, db):
    from fanclub.models import Creator

    creator = Creator.objects.create(name="検査", slug="date-check")
    resp = staff_client.post(
        f"/api/v1/admin/fanclub/creators/{creator.pk}/contracts",
        {"title": "t", "monthly_fee_minor": 1, "starts_on": "not-a-date"},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_series_post_invalid_datetime_is_422_not_500(staff_client, channel):
    from scheduling.models import Series

    series = Series.objects.create(channel=channel, title="検査")
    resp = staff_client.post(
        f"/api/v1/admin/scheduling/{channel.slug}/series/{series.pk}/posts",
        {"title": "t", "campaign_start": "2026-13-45T00:00:00"},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_procurement_payment_invalid_date_is_422_not_500(staff_client, db):
    _skip_unless_route("/api/v1/admin/procurement/costs/{cost_id}/pay")
    import datetime

    from procurement.models import DeliveryCost

    cost = DeliveryCost.objects.create(
        delivery_id=1, delivery_title="検査", amount=1000, incurred_on=datetime.date(2026, 6, 1)
    )
    for bad in ("", "not-a-date", "2026-13-45"):
        resp = staff_client.post(
            f"/api/v1/admin/procurement/costs/{cost.pk}/pay",
            {"paid_amount": 100, "paid_at": bad},
            content_type="application/json",
        )
        assert resp.status_code == 422, bad


def test_series_post_negative_level_is_4xx_not_500(staff_client, channel):
    from scheduling.models import Series

    series = Series.objects.create(channel=channel, title="検査")
    resp = staff_client.post(
        f"/api/v1/admin/scheduling/{channel.slug}/series/{series.pk}/posts",
        {"title": "t", "fc_required_level": -1},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_db_constraint_errors_are_4xx_json(rf):
    # 仕様に現れない制約 (PositiveIntegerField の下限など) に反した入力は、500 でなく 4xx で返す
    from django.db import DataError, IntegrityError

    from api.api import internal_admin_api, public_api

    for api in (public_api, internal_admin_api):
        for exc_type, status in ((IntegrityError, 409), (DataError, 400)):
            handler = api._exception_handlers[exc_type]
            resp = handler(rf.get("/api/v1/x"), exc_type("boom"))
            assert resp.status_code == status
            assert resp["Content-Type"].startswith("application/json")
            assert "detail" in json.loads(resp.content)


def test_breaking_telop_without_enabled_channel_does_not_crash(db, settings):
    from core.views import fire_breaking_telop

    # 有効なチャンネルが 0 件でも chime 指定で UnboundLocalError (disc) にならない
    assert fire_breaking_telop([], "text", op="show", chime="eew") == []


def test_delivery_asset_unknown_series_is_400_not_500(db, client, settings):
    settings.DELIVERY_REGISTER_TOKEN = "t"  # pragma: allowlist secret - test only
    resp = client.post(
        "/api/v1/internal/delivery-asset",
        {
            "r2_key": "deliveries/x.mp4",
            "title": "t",
            "kind": "program",
            "series_id": 987654321,
            "episode_no": 1,
            "duration_ms": 1000,
        },
        content_type="application/json",
        HTTP_X_INTERNAL_TOKEN="t",
    )
    assert resp.status_code == 400
