# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理: 退会 (アカウント削除。パスワード再入力 + PII ハード削除)。"""

from __future__ import annotations

from django.test import override_settings

from members.auth import SESSION_KEY
from members.models import CodePurpose, Member, MemberEmailCode

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-wrong-1!"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


@_PUBLIC_HOST
def test_delete_requires_login(http_client, db):
    res = http_client.get("/members/delete/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_delete_removes_member_and_codes_and_session(http_client, db):
    m = _make_member()
    MemberEmailCode.objects.create(
        member=m, purpose=CodePurpose.EMAIL_VERIFY, code_hash="x", expires_at=m.created_at
    )
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 302 and res.url == "/"
    assert not Member.objects.filter(pk=m.pk).exists()
    assert not MemberEmailCode.objects.filter(member_id=m.pk).exists()  # CASCADE
    assert SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_delete_wrong_password_keeps_account(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _WRONG, "confirm": "on"})
    assert res.status_code == 200
    assert Member.objects.filter(pk=m.pk).exists()


@_PUBLIC_HOST
def test_delete_requires_confirm_checkbox(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW})  # confirm 無し
    assert res.status_code == 200
    assert Member.objects.filter(pk=m.pk).exists()
