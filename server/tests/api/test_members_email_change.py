# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理: メールアドレス変更 (新アドレスへコード → 確認で確定・再検証)。"""

from __future__ import annotations

import re

from django.core import mail
from django.test import override_settings

from members import codes
from members.models import CodePurpose, Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")

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
def test_email_change_requires_login(http_client, db):
    res = http_client.get("/members/email/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
@_LOCMEM
def test_email_change_sends_to_new_address(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/email/", {"new_email": "new@example.com", "password": _PW})
    assert res.status_code == 302 and res.url == "/members/email/confirm/"
    m.refresh_from_db()
    assert m.pending_email == "new@example.com"
    assert m.email == "m@example.com"  # 確認前は未変更
    assert len(mail.outbox) == 1 and "new@example.com" in mail.outbox[0].to  # 新アドレス宛


@_PUBLIC_HOST
@_LOCMEM
def test_email_change_confirm_applies(http_client, db):
    m = _make_member()
    _login(http_client)
    http_client.post("/members/email/", {"new_email": "new@example.com", "password": _PW})
    code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
    res = http_client.post("/members/email/confirm/", {"code": code})
    assert res.status_code == 302 and res.url == "/members/"
    m.refresh_from_db()
    assert m.email == "new@example.com" and m.pending_email is None
    assert m.email_verified_at is not None  # 新アドレスは確認済


@_PUBLIC_HOST
@_LOCMEM
def test_email_change_wrong_password_rejected(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/email/", {"new_email": "new@example.com", "password": _WRONG})
    assert res.status_code == 200
    m.refresh_from_db()
    assert m.pending_email is None and len(mail.outbox) == 0


@_PUBLIC_HOST
@_LOCMEM
def test_email_change_to_taken_address_rejected(http_client, db):
    _make_member()
    _make_member(email="taken@example.com")
    _login(http_client)
    res = http_client.post("/members/email/", {"new_email": "taken@example.com", "password": _PW})
    assert res.status_code == 200  # 既使用で弾く
    assert Member.objects.get(email="m@example.com").pending_email is None


@_PUBLIC_HOST
@_LOCMEM
def test_email_change_wrong_code_keeps_old(http_client, db):
    m = _make_member()
    _login(http_client)
    codes.issue_code(m, CodePurpose.EMAIL_CHANGE)
    Member.objects.filter(pk=m.pk).update(pending_email="new@example.com")
    res = http_client.post("/members/email/confirm/", {"code": "000000"})
    assert res.status_code == 200
    m.refresh_from_db()
    assert m.email == "m@example.com" and m.pending_email == "new@example.com"
