# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H-1: ログイン throttle が Cookie 非依存 (email+IP・DB) であることの回帰テスト。

旧実装は失敗回数を `request.session` に持っていたため、Cookie を毎回捨てれば無制限に
総当たり/クレデンシャルスタッフィングできた (docs/security-review.md H-1)。新実装は
AuthThrottle (DB) に email(HMAC)/IP 別で数えるため、新しい Client (=新セッション) でも
ロックが効く。ここではその核心を「毎回まっさらな Client」で検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from members import throttle
from members.auth import SESSION_KEY
from members.models import AuthThrottle, Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-wrong-1!"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com"):
    m = Member(
        email=email,
        nickname="t",
        birth_year=1988,
        birth_month=6,
        gender="male",
        postal_code="1500001",
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(client, email, password, ip="203.0.113.9"):
    # 毎回 XFF を渡し client_ip を決定的にする (既定は XFF 最左)。
    return client.post(
        "/members/login/",
        {"email": email, "password": password},
        HTTP_X_FORWARDED_FOR=ip,
    )


@_PUBLIC_HOST
@override_settings(ICSTV_LOGIN_FAIL_MAX=3, ICSTV_LOGIN_IP_FAIL_MAX=99)
def test_email_lock_survives_cookie_drop(db):
    """新しい Client (Cookie 破棄) で失敗を重ねてもロックが効く = H-1 の核心。"""
    _make_member()
    for _ in range(3):  # 毎回まっさらな Client = session Cookie 無し
        c = Client()
        res = _login(c, "m@example.com", _WRONG)
        assert res.status_code == 200
        assert SESSION_KEY not in c.session
    # 上限到達後は正パスワードでも email ロックで弾く (別 Client=Cookie 無しでも)
    c = Client()
    res = _login(c, "m@example.com", _PW)
    assert res.status_code == 200
    assert SESSION_KEY not in c.session


@_PUBLIC_HOST
@override_settings(ICSTV_LOGIN_FAIL_MAX=3, ICSTV_LOGIN_IP_FAIL_MAX=99)
def test_successful_login_clears_email_counter(db):
    _make_member()
    for _ in range(2):  # 上限未満
        _login(Client(), "m@example.com", _WRONG)
    c = Client()
    res = _login(c, "m@example.com", _PW)  # 成功
    assert res.status_code == 302 and c.session[SESSION_KEY]
    assert not AuthThrottle.objects.filter(
        scope=throttle.LOGIN, key=throttle.hash_email("m@example.com")
    ).exists()


@_PUBLIC_HOST
@override_settings(ICSTV_LOGIN_FAIL_MAX=3, ICSTV_LOGIN_IP_FAIL_MAX=99)
def test_other_email_not_locked(db):
    """email A のロックは email B に波及しない (IP 上限は十分高い)。"""
    _make_member(email="a@example.com")
    b = _make_member(email="b@example.com")
    for _ in range(3):
        _login(Client(), "a@example.com", _WRONG)
    c = Client()
    res = _login(c, "b@example.com", _PW)
    assert res.status_code == 302 and c.session[SESSION_KEY] == b.pk


@_PUBLIC_HOST
@override_settings(ICSTV_LOGIN_FAIL_MAX=99, ICSTV_LOGIN_IP_FAIL_MAX=4)
def test_ip_lock_across_emails(db):
    """同一 IP からの総当たりは email を変えても IP 上限で止まる (スタッフィング抑止)。"""
    valid = _make_member(email="real@example.com")
    for i in range(4):
        _login(Client(), f"probe{i}@example.com", _WRONG, ip="198.51.100.7")
    # IP ロック済 → 有効会員の正パスワードでも同一 IP なら弾く
    c = Client()
    res = _login(c, "real@example.com", _PW, ip="198.51.100.7")
    assert res.status_code == 200 and SESSION_KEY not in c.session
    # 別 IP なら通る
    c2 = Client()
    res2 = _login(c2, "real@example.com", _PW, ip="198.51.100.8")
    assert res2.status_code == 302 and c2.session[SESSION_KEY] == valid.pk


def test_throttle_unit_window_and_clear(db):
    """throttle モジュールの window リセット/clear の単体挙動。"""
    with override_settings(ICSTV_LOGIN_FAIL_MAX=2, ICSTV_LOGIN_FAIL_WINDOW=900):
        throttle.record_failure(throttle.LOGIN, "k1")
        throttle.record_failure(throttle.LOGIN, "k1")
        assert throttle.is_locked(throttle.LOGIN, "k1")
        # window を過去へ巻くと解ける
        AuthThrottle.objects.filter(scope=throttle.LOGIN, key="k1").update(
            window_start=timezone.now() - timedelta(seconds=1000)
        )
        assert not throttle.is_locked(throttle.LOGIN, "k1")
        # clear で消える
        throttle.record_failure(throttle.LOGIN, "k1")
        throttle.clear(throttle.LOGIN, "k1")
        assert not AuthThrottle.objects.filter(scope=throttle.LOGIN, key="k1").exists()


def test_hash_email_no_plaintext():
    """throttle 行に生メールを残さない (HMAC ハッシュ・正規化)。"""
    h = throttle.hash_email("Secret@Example.com")
    assert "@" not in h and "secret" not in h and len(h) == 64
    assert h == throttle.hash_email("  secret@example.com  ")  # trim + lower を正規化
