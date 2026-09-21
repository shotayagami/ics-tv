# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理: パスワードリセット (メールコードで再設定・列挙対策)。"""

from __future__ import annotations

import re

from django.core import mail
from django.test import override_settings

from members.models import CodePurpose, Member, MemberEmailCode

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_NEW = "Zz8@mqp42hdk"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


@_PUBLIC_HOST
@_LOCMEM
def test_reset_request_sends_code_for_existing(http_client, db):
    _make_member()
    res = http_client.post("/members/password/reset/", {"email": "m@example.com"})
    assert res.status_code == 302 and res.url.startswith("/members/password/reset/confirm/")
    assert len(mail.outbox) == 1
    assert MemberEmailCode.objects.filter(purpose=CodePurpose.PASSWORD_RESET).count() == 1


@_PUBLIC_HOST
@_LOCMEM
def test_reset_request_unknown_email_no_send_same_response(http_client, db):
    res = http_client.post("/members/password/reset/", {"email": "nobody@example.com"})
    # 列挙対策: 存在しなくても同じ 302 (confirm へ) ・メールは送らない
    assert res.status_code == 302 and res.url.startswith("/members/password/reset/confirm/")
    assert len(mail.outbox) == 0
    assert not MemberEmailCode.objects.exists()


@_PUBLIC_HOST
@_LOCMEM
def test_reset_confirm_changes_password(http_client, db):
    from members import codes

    m = _make_member()
    code = codes.issue_code(m, CodePurpose.PASSWORD_RESET)
    res = http_client.post(
        "/members/password/reset/confirm/",
        {
            "email": "m@example.com",
            "code": code,
            "new_password": _NEW,
            "new_password_confirm": _NEW,
        },
    )
    assert res.status_code == 302 and res.url == "/members/login/"
    m.refresh_from_db()
    assert m.check_password(_NEW) and not m.check_password(_PW)


@_PUBLIC_HOST
@_LOCMEM
def test_reset_confirm_wrong_code_rejected(http_client, db):
    m = _make_member()
    res = http_client.post(
        "/members/password/reset/confirm/",
        {
            "email": "m@example.com",
            "code": "000000",
            "new_password": _NEW,
            "new_password_confirm": _NEW,
        },
    )
    assert res.status_code == 200  # 再表示 (失敗)
    m.refresh_from_db()
    assert m.check_password(_PW)  # 変わっていない


@_PUBLIC_HOST
@_LOCMEM
def test_reset_full_flow_via_email_code(http_client, db):
    _make_member()
    http_client.post("/members/password/reset/", {"email": "m@example.com"})
    code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
    res = http_client.post(
        "/members/password/reset/confirm/",
        {
            "email": "m@example.com",
            "code": code,
            "new_password": _NEW,
            "new_password_confirm": _NEW,
        },
    )
    assert res.status_code == 302
    # 新パスワードでログインできる
    login = http_client.post("/members/login/", {"email": "m@example.com", "password": _NEW})
    assert login.status_code == 302 and login.url == "/members/"
