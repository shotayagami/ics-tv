# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""API が OpenAPI 仕様どおりに応答することを検査する (schemathesis)。

仕様から生成した入力を in-process の WSGI アプリへ送り、次を確かめる。

- 5xx を返さない (`not_a_server_error`)
- 応答のステータス・content-type・body が仕様に適合する (`status_code_conformance` ほか)

対象は次のとおり。副作用のない GET に限り、認証付きは既存 fixture の認証情報で呼ぶ。

- security を持たない operation (全メソッド)
- staff セッションで通る GET (`staff_client` のセッション cookie)
- 会員トークンで通る GET (`api_tokens.issue` のトークン)

次は未対応。トークン系 (Backoffice / Delivery / Monitor の GET)、副作用のある POST / PUT /
DELETE (テストデータの後始末と、入力の生成範囲を決める必要がある)。
"""

import contextlib

import pytest
import schemathesis
from django.conf import settings as dj_settings
from django.core import signals
from django.db import close_old_connections
from django.test import override_settings
from django.utils import timezone
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import ignored_auth

from api.management.commands.dump_openapi import _merged_schema
from config.wsgi import application

# werkzeug のテストクライアントは、自前の cookie jar を持つため、リクエストの Cookie ヘッダを
# 捨てる (ヘッダで渡すと黙って未認証 = 401 になる)。認証用の cookie は別名ヘッダで渡し、
# アプリの手前で HTTP_COOKIE に戻す。
_COOKIE_HEADER = "X-Test-Cookie"


def _app_with_cookie_header(environ, start_response):
    forwarded = environ.pop("HTTP_X_TEST_COOKIE", None)
    if forwarded:
        environ["HTTP_COOKIE"] = forwarded
    return application(environ, start_response)


_schema = schemathesis.openapi.from_dict(_merged_schema())
_schema.app = _app_with_cookie_header
# security_parameters: 認証 cookie は上記の別名ヘッダで渡すので、schemathesis に生成・欠落
# させない (欠落を「不正入力」と見なして受理を誤検出する)。
# allow_x00: NUL 文字は JSON として正当だが PostgreSQL の text に入らず、`DataError` を 400 に
# している。仕様は文字列の内容を制約しないため「受理すべき入力」と判定される。
# ponytail: NUL の扱いは test_openapi_errors の回帰テストで固定。仕様側で表現できたら外す
_schema.config.generation.update(with_security_parameters=False, allow_x00=False)


@pytest.fixture(autouse=True)
def _keep_db_connection():
    """WSGI ハンドラは request_started/finished で接続を閉じ、テストのトランザクションが
    壊れる。Django のテストクライアントと同じく、テスト中だけ切り離す。"""
    signals.request_started.disconnect(close_old_connections)
    signals.request_finished.disconnect(close_old_connections)
    yield
    signals.request_started.connect(close_old_connections)
    signals.request_finished.connect(close_old_connections)


def _alternatives(ctx) -> list[set[str]]:
    """security は OR の配列 (各要素のスキーム名の集合)。"""
    return [set(a) for a in ctx.operation.definition.raw.get("security", [])]


def _is_public(ctx) -> bool:
    return not ctx.operation.definition.raw.get("security")


def _is_get(ctx) -> bool:
    return ctx.operation.method.upper() == "GET"


def _staff_get(ctx) -> bool:
    return _is_get(ctx) and {"SessionAuthIsStaff"} in _alternatives(ctx)


def _member_get(ctx) -> bool:
    return _is_get(ctx) and {"MemberTokenAuth"} in _alternatives(ctx)


def _assert_authenticated(response) -> None:
    """未認証 (401) のまま「仕様に適合」と判定される空振りを防ぐ。

    401 は認証が効いていないことを意味する。認証の効いた呼び出しが 401 を返したら失敗にする。
    """
    assert response.status_code != 401, "認証が効いていない (401)"


_SETTINGS = settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
# テストクライアントの既定ホスト (testserver) は管理ホスト扱いで api/v1/ が解決される
_HOST = {"Host": "testserver"}


@_schema.include(func=_is_public).parametrize()
@_SETTINGS
@pytest.mark.django_db
def test_public_operation_conforms_to_spec(case):
    case.call_and_validate(headers=_HOST)


@_schema.include(func=_staff_get).parametrize()
@_SETTINGS
@pytest.mark.django_db
def test_staff_get_conforms_to_spec(case, staff_client, channel):
    name = dj_settings.SESSION_COOKIE_NAME
    # 生成した slug は存在せず 404 になるだけなので、実在のチャンネルへ差し替える
    # (ID 系のパラメータは未対応。404 の形式だけを見る)
    if "slug" in (case.path_parameters or {}):
        case.path_parameters["slug"] = channel.slug
    cookie = f"{name}={staff_client.cookies[name].value}"
    response = case.call(headers={**_HOST, _COOKIE_HEADER: cookie})
    _assert_authenticated(response)
    # ignored_auth は認証を外して再送する検査。認証を別名ヘッダで渡している (上記) ため
    # 外せず、常に「認証なしで通る」と誤検出する。401 の宣言は test_openapi_errors が見る
    case.validate_response(response, excluded_checks=[ignored_auth])


@pytest.fixture
def member_token(db, request):
    """会員とそのトークン。hypothesis は 1 つの関数を例ごとに繰り返し呼ぶため、
    unique 制約のある行は関数内でなく fixture (関数あたり 1 回) で作る。"""
    with contextlib.suppress(pytest.FixtureLookupError):
        request.getfixturevalue("owner_tenant")  # テナント分離のあるツリーだけに存在する
    from members import api_tokens
    from members.models import Member

    member = Member.objects.create(
        email="conformance@example.com",
        nickname="検査",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        email_verified_at=timezone.now(),
    )
    return api_tokens.issue(member)


@_schema.include(func=_member_get).parametrize()
@_SETTINGS
@pytest.mark.django_db
@override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
def test_member_get_conforms_to_spec(case, member_token):
    response = case.call(headers={**_HOST, "Authorization": f"Bearer {member_token}"})
    _assert_authenticated(response)
    # ignored_auth は認証を外して再送する検査。認証を別名ヘッダで渡している (上記) ため
    # 外せず、常に「認証なしで通る」と誤検出する。401 の宣言は test_openapi_errors が見る
    case.validate_response(response, excluded_checks=[ignored_auth])
