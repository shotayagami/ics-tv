# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理 commit② : メール確認コード (発行/送信/検証/レート制限)。

メールは locmem backend で outbox を検証。コード平文は issue_code の戻り値で取得する
(DB には make_password ハッシュしか残らない)。会員ルートは公開 urlconf 専用なので
override_settings(ICSTV_ADMIN_HOSTS=[]) で既定ホスト testserver を公開側へ倒す。
"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from members import codes
from members.models import CodePurpose, Member, MemberEmailCode

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


@_PUBLIC_HOST
@_LOCMEM
def test_register_sends_code_and_lands_on_verify(http_client, db):
    res = http_client.post(
        "/members/register/",
        {
            "email": "v@example.com",
            "password": _PW,
            "password_confirm": _PW,
            "nickname": "v",
            "birth_year": 1990,
            "birth_month": 4,
            "gender": "no_answer",
            "country": "JP",
            "postal_code": "1000001",
            "agree": "on",
        },
    )
    assert res.status_code == 302 and res.url == "/members/verify/email/"
    assert len(mail.outbox) == 1
    assert "v@example.com" in mail.outbox[0].to
    rec = MemberEmailCode.objects.get(purpose=CodePurpose.EMAIL_VERIFY)
    assert rec.code_hash and rec.consumed_at is None  # ハッシュ保管・未消費


def test_confirm_marks_email_verified(db):
    m = _make_member()
    code = codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
    assert codes.verify_code(m, CodePurpose.EMAIL_VERIFY, code) is True
    # consume 済 → 同じコードは再利用不可 (単回)
    assert codes.verify_code(m, CodePurpose.EMAIL_VERIFY, code) is False


@_PUBLIC_HOST
@_LOCMEM
def test_confirm_view_flips_is_verified(http_client, db):
    m = _make_member()
    _login(http_client)
    code = codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
    res = http_client.post("/members/verify/email/confirm/", {"code": code})
    assert res.status_code == 302 and res.url == "/members/"
    m.refresh_from_db()
    assert m.email_verified_at is not None and m.is_verified


def test_wrong_code_increments_attempts_then_locks(db):
    m = _make_member()
    codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
    for _ in range(5):
        assert codes.verify_code(m, CodePurpose.EMAIL_VERIFY, "000000") is False
    rec = MemberEmailCode.objects.get(member=m)
    assert rec.attempts == 5
    # 上限到達後はそのコードはもう通らない (別の有効コードも無い)
    assert codes.verify_code(m, CodePurpose.EMAIL_VERIFY, "123456") is False


def test_expired_code_rejected(db):
    m = _make_member()
    code = codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
    MemberEmailCode.objects.filter(member=m).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert codes.verify_code(m, CodePurpose.EMAIL_VERIFY, code) is False


@override_settings(ICSTV_MEMBER_CODE_SEND_COOLDOWN=60)
def test_send_cooldown_throttles(db):
    m = _make_member()
    codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
    try:
        codes.issue_code(m, CodePurpose.EMAIL_VERIFY)
        raise AssertionError("expected SendThrottledError")
    except codes.SendThrottledError as e:
        assert e.retry_after > 0


@_PUBLIC_HOST
@_LOCMEM
def test_resend_view_sends_again(http_client, db):
    _make_member()
    _login(http_client)
    mail.outbox.clear()
    res = http_client.post("/members/verify/email/send/")
    assert res.status_code == 302 and res.url == "/members/verify/email/"
    assert len(mail.outbox) == 1
