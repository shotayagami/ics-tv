# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""§3: セキュリティ構造化ログ (icstv.security) の発火テスト。

icstv.security は propagate=False なので、caplog のハンドラを当該ロガーへ直接付けて捕捉する。
検証観点: emit が妥当な JSON を出す / None フィールドを落とす / 平文メールを載せない /
ログイン失敗・internal トークン失敗が発火する。
"""

from __future__ import annotations

import json
import logging

import pytest
from django.test import override_settings

from core.security_log import emit
from members.models import Member

_PUBLIC = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


@pytest.fixture
def sec_logs(caplog):
    caplog.set_level(logging.INFO, logger="icstv.security")
    lg = logging.getLogger("icstv.security")
    lg.addHandler(caplog.handler)
    yield caplog
    lg.removeHandler(caplog.handler)


def _events(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "icstv.security"]


def _member():
    m = Member(
        email="m@example.com", nickname="t", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password(_PW)
    m.save()
    return m


def test_emit_produces_valid_json_and_drops_none(sec_logs):
    emit("test.event", outcome="ok", foo="bar", none_field=None)
    ev = _events(sec_logs)[-1]
    assert ev["event"] == "test.event" and ev["outcome"] == "ok" and ev["foo"] == "bar"
    assert "none_field" not in ev  # None は落とす


@_PUBLIC
def test_login_failure_emits_event_without_plaintext_email(http_client, db, sec_logs):
    _member()
    http_client.post(
        "/members/login/",
        {"email": "m@example.com", "password": "wrong-pass-1!"},  # pragma: allowlist secret
        HTTP_X_FORWARDED_FOR="203.0.113.5",
    )
    fail = [
        e for e in _events(sec_logs) if e["event"] == "auth.member.login" and e["outcome"] == "fail"
    ]
    assert fail, "ログイン失敗イベントが出ていない"
    assert fail[0]["ip"] == "203.0.113.5"
    assert "email" not in fail[0] and "email_hash" in fail[0]  # 平文メールは載せない


@_PUBLIC
def test_login_success_emits_event(http_client, db, sec_logs):
    m = _member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    ok = [
        e
        for e in _events(sec_logs)
        if e["event"] == "auth.member.login" and e["outcome"] == "success"
    ]
    assert ok and ok[0]["member_id"] == m.pk


def test_internal_token_fail_emits_event(http_client, db, sec_logs):
    # 管理ホスト (testserver) で internal を叩く。トークン無し → 401 + emit。
    http_client.post("/api/v1/internal/weather-import", data="{}", content_type="application/json")
    tok = [e for e in _events(sec_logs) if e["event"] == "internal.token"]
    assert tok and tok[0]["token_kind"] == "weather" and tok[0]["outcome"] == "fail"
