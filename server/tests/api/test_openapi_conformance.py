# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開 API (認証不要の operation) が OpenAPI 仕様どおりに応答することを検査する (schemathesis)。

仕様から生成した入力を in-process の WSGI アプリへ送り、次を確かめる。

- 5xx を返さない (`not_a_server_error`)
- 応答のステータス・content-type・body が仕様に適合する (`status_code_conformance` ほか)

対象は security を持たない operation に限る。認証付きの operation は、テスト用の認証情報を
用意できた段階で範囲を広げる。
"""

import pytest
import schemathesis
from django.core import signals
from django.db import close_old_connections
from hypothesis import HealthCheck, settings

from api.management.commands.dump_openapi import _merged_schema
from config.wsgi import application

_schema = schemathesis.openapi.from_dict(_merged_schema())
_schema.app = application


@pytest.fixture(autouse=True)
def _keep_db_connection():
    """WSGI ハンドラは request_started/finished で接続を閉じ、テストのトランザクションが
    壊れる。Django のテストクライアントと同じく、テスト中だけ切り離す。"""
    signals.request_started.disconnect(close_old_connections)
    signals.request_finished.disconnect(close_old_connections)
    yield
    signals.request_started.connect(close_old_connections)
    signals.request_finished.connect(close_old_connections)


def _is_public(ctx) -> bool:
    return not ctx.operation.definition.raw.get("security")


@_schema.include(func=_is_public).parametrize()
@settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
@pytest.mark.django_db
def test_public_operation_conforms_to_spec(case):
    # テストクライアントの既定ホスト (testserver) は管理ホスト扱いで api/v1/ が解決される
    response = case.call(headers={"Host": "testserver"})
    # パスのコンバータ (`{int:...}`) に合わない値は、ninja に届く前に Django の URL ルーターが
    # HTML の 404 を返す。仕様の対象外なので、ninja が返さない形式 (HTML) の 404 だけを許す。
    # ponytail: 24 ルートの `{int:}` を外して 422 (JSON) に揃えれば、この許容は不要になる
    if response.status_code == 404 and "text/html" in response.headers.get("content-type", [""])[0]:
        return
    case.validate_response(response)
