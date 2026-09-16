# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理 commit③ : TOTP / メール 2FA + 未認証ゲート。

TOTP は固定/生成秘密から pyotp で決定的にコードを計算して検証する。メール2FA は locmem
outbox の本文から6桁を取り出して通す。会員ルートは公開 urlconf 専用なので override で倒す。
"""

from __future__ import annotations

import re

import pyotp
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from members import totp
from members.auth import SESSION_KEY
from members.models import Member, TwoFactorMethod

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-1!"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com", **over):
    m = Member(
        email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001", **over
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


def _totp_enrolled(**over):
    secret = totp.new_secret()
    m = _make_member(
        totp_secret=secret,
        totp_confirmed_at=timezone.now(),
        two_factor_method=TwoFactorMethod.TOTP,
        **over,
    )
    return m, secret


def test_verify_totp_helper(db):
    s = totp.new_secret()
    assert totp.verify_totp(s, pyotp.TOTP(s).now()) is True
    assert totp.verify_totp(s, "000000") is False
    assert totp.verify_totp(None, "123456") is False


@_PUBLIC_HOST
def test_totp_enrollment(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.get("/members/2fa/totp/setup/")
    assert res.status_code == 200
    secret = http_client.session["member_totp_setup_secret"]
    res2 = http_client.post("/members/2fa/totp/setup/", {"code": pyotp.TOTP(secret).now()})
    assert res2.status_code == 302 and res2.url == "/members/2fa/"
    m.refresh_from_db()
    assert m.totp_confirmed_at is not None
    assert m.two_factor_method == TwoFactorMethod.TOTP
    assert m.is_verified  # TOTP 確定でも確認済になる


@_PUBLIC_HOST
def test_totp_enrollment_wrong_code(http_client, db):
    m = _make_member()
    _login(http_client)
    http_client.get("/members/2fa/totp/setup/")
    res = http_client.post("/members/2fa/totp/setup/", {"code": "000000"})
    assert res.status_code == 200  # 再表示
    m.refresh_from_db()
    assert m.totp_confirmed_at is None


@_PUBLIC_HOST
def test_login_requires_totp(http_client, db):
    m, secret = _totp_enrolled()
    res = http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    assert res.status_code == 302 and res.url == "/members/login/2fa/totp/"
    assert SESSION_KEY not in http_client.session  # 第2要素前はログインしていない
    res2 = http_client.post("/members/login/2fa/totp/", {"code": pyotp.TOTP(secret).now()})
    assert res2.status_code == 302 and res2.url == "/members/"
    assert http_client.session[SESSION_KEY] == m.pk


@_PUBLIC_HOST
def test_login_totp_wrong_code_blocks(http_client, db):
    _totp_enrolled()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.post("/members/login/2fa/totp/", {"code": "000000"})
    assert res.status_code == 200
    assert SESSION_KEY not in http_client.session


@_PUBLIC_HOST
@override_settings(ICSTV_TOTP_FAIL_MAX=3, ICSTV_TOTP_IP_FAIL_MAX=99)
def test_login_totp_bruteforce_locks(http_client, db):
    """M-1: TOTP 第2要素に試行上限。上限到達後は正しいコードでも弾く (2FA バイパス防止)。"""
    _m, secret = _totp_enrolled()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})  # pending
    for _ in range(3):
        r = http_client.post("/members/login/2fa/totp/", {"code": "000000"})
        assert r.status_code == 200 and SESSION_KEY not in http_client.session
    # 上限到達 → 正コードでもロックで拒否
    r = http_client.post("/members/login/2fa/totp/", {"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200 and SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_login_totp_replay_rejected(http_client, db):
    """M-1: 一度使った TOTP コードは (有効窓内でも) 再利用できない。"""
    m, secret = _totp_enrolled()
    code = pyotp.TOTP(secret).now()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    r1 = http_client.post("/members/login/2fa/totp/", {"code": code})
    assert r1.status_code == 302 and http_client.session[SESSION_KEY] == m.pk
    m.refresh_from_db()
    assert m.totp_last_step is not None  # 使用ステップを記録
    # ログアウトして同一コードで再ログイン → リプレイ拒否
    http_client.post("/members/logout/")
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    r2 = http_client.post("/members/login/2fa/totp/", {"code": code})
    assert r2.status_code == 200 and SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_two_factor_disable_clears_totp(http_client, db):
    m, secret = _totp_enrolled(email_verified_at=timezone.now())
    # 2FA を通してログイン
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    http_client.post("/members/login/2fa/totp/", {"code": pyotp.TOTP(secret).now()})
    res = http_client.post("/members/2fa/disable/", {"password": _PW})
    assert res.status_code == 302
    m.refresh_from_db()
    assert m.two_factor_method == TwoFactorMethod.NONE
    assert not m.totp_secret and m.totp_confirmed_at is None
    assert m.is_verified  # email_verified_at は残るので確認済のまま


@_PUBLIC_HOST
def test_two_factor_disable_wrong_password(http_client, db):
    m, secret = _totp_enrolled()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    http_client.post("/members/login/2fa/totp/", {"code": pyotp.TOTP(secret).now()})
    res = http_client.post("/members/2fa/disable/", {"password": _WRONG})
    assert res.status_code == 302  # 設定ページへ戻る (失敗)
    m.refresh_from_db()
    assert m.two_factor_method == TwoFactorMethod.TOTP  # 変わっていない


@_PUBLIC_HOST
@_LOCMEM
def test_email_2fa_login_flow(http_client, db):
    m = _make_member(email_verified_at=timezone.now(), two_factor_method=TwoFactorMethod.EMAIL)
    res = http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    assert res.status_code == 302 and res.url == "/members/login/2fa/email/"
    assert SESSION_KEY not in http_client.session
    assert len(mail.outbox) == 1
    code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
    res2 = http_client.post("/members/login/2fa/email/", {"code": code})
    assert res2.status_code == 302 and res2.url == "/members/"
    assert http_client.session[SESSION_KEY] == m.pk


@_PUBLIC_HOST
def test_two_factor_settings_page_renders(http_client, db):
    _make_member(email_verified_at=timezone.now())
    _login(http_client)
    res = http_client.get("/members/2fa/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "2段階認証" in body and "認証アプリを登録する" in body


@_PUBLIC_HOST
@_LOCMEM
def test_email_2fa_challenge_page_renders(http_client, db):
    _make_member(email_verified_at=timezone.now(), two_factor_method=TwoFactorMethod.EMAIL)
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.get("/members/login/2fa/email/")  # pending 中の GET
    assert res.status_code == 200
    assert "確認コード" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_verify_required_page(http_client, db):
    m = _make_member()  # 未認証
    _login(http_client)
    res = http_client.get("/members/verify-required/")
    assert res.status_code == 200
    assert "本人確認が必要です" in res.content.decode("utf-8")
    # 確認済になったら通常ページへ流す
    m.email_verified_at = timezone.now()
    m.save(update_fields=["email_verified_at"])
    res2 = http_client.get("/members/verify-required/")
    assert res2.status_code == 302 and res2.url == "/members/"
