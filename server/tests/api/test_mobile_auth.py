# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ネイティブアプリ トークン認証 API (#MOBILE-01)。

HTML ログインと同じ防御 (IP/メール/第2要素のスロットル、タイミング平準化、TOTP リプレイ
拒否) がトークン経路にも掛かっていることを主に確認する。片方だけ緩い迂回路ができると
2FA を素通りできてしまうため、ここは網羅的に見る。
"""

from __future__ import annotations

import re
from datetime import timedelta

import pyotp
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from members import throttle, totp
from members.models import (
    AuthThrottle,
    Member,
    MemberApiToken,
    MemberLoginChallenge,
    TwoFactorMethod,
)

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-1!"  # pragma: allowlist secret - test only

_LOGIN = "/api/v1/auth/login"
_LOGIN_2FA = "/api/v1/auth/login/2fa"
_RESEND = "/api/v1/auth/login/2fa/resend"
_LOGOUT = "/api/v1/auth/logout"
_ME = "/api/v1/auth/me"


def _member(email="m@example.com", verified=True, **over):
    m = Member(
        email=email,
        nickname="みんと",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        **over,
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _post(http_client, path, payload):
    return http_client.post(path, payload, content_type="application/json")


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ---- 第1要素のみ (2FA 未設定) ----


@_PUBLIC_HOST
def test_login_without_2fa_issues_token(db, http_client):
    _member()
    res = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["token"]
    assert body["member"]["nickname"] == "みんと"
    assert MemberApiToken.objects.count() == 1
    # 平文はレスポンスにしか出ない (DB はハッシュのみ)。
    assert MemberApiToken.objects.get().token_hash != body["token"]


@_PUBLIC_HOST
def test_issued_token_authenticates_me(db, http_client):
    _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]
    res = http_client.get(_ME, **_auth(token))
    assert res.status_code == 200
    assert res.json()["email"] == "m@example.com"


@_PUBLIC_HOST
def test_me_requires_auth(db, http_client):
    assert http_client.get(_ME).status_code == 401


@_PUBLIC_HOST
def test_token_authorizes_post_without_csrf(db, channel):
    """アプリは CSRF トークンを持てない。Bearer なら非 GET も通ること (cookie 版は従来どおり)。

    既定の http_client (Client(), enforce_csrf_checks=False) では CSRF が最初から無効化されて
    おり、このケースの本質 (Bearer だけで CSRF チェックそのものを回避できるか) を検証できない
    — 実際に CSRF を強制する Client を使う。より広い comments/pin の網羅は
    test_mobile_comments.py。
    """
    from django.test import Client

    _member()
    login_client = Client(enforce_csrf_checks=True)
    token = login_client.post(
        _LOGIN, {"email": "m@example.com", "password": _PW}, content_type="application/json"
    ).json()["token"]

    csrf_client = Client(enforce_csrf_checks=True)
    res = csrf_client.post(
        f"/api/v1/channels/{channel.slug}/pin", content_type="application/json", **_auth(token)
    )
    assert res.status_code == 200
    assert res.json()["pinned"] is True


# ---- 資格情報の失敗 ----


@_PUBLIC_HOST
def test_wrong_password_is_401_and_counts_failure(db, http_client):
    _member()
    res = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _WRONG})
    assert res.status_code == 401
    assert MemberApiToken.objects.count() == 0
    assert AuthThrottle.objects.filter(scope=throttle.LOGIN).exists()


@_PUBLIC_HOST
def test_unknown_email_is_401_same_as_wrong_password(db, http_client):
    """存在しないメールでも同じ 401/本文 (会員の存在を漏らさない)。"""
    _member()
    known = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _WRONG})
    unknown = _post(http_client, _LOGIN, {"email": "nobody@example.com", "password": _WRONG})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()


@_PUBLIC_HOST
def test_inactive_member_cannot_login(db, http_client):
    _member(is_active=False)
    assert (
        _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).status_code == 401
    )


@_PUBLIC_HOST
def test_email_lock_blocks_with_429(db, http_client):
    _member()
    key = throttle.hash_email("m@example.com")
    for _ in range(20):
        throttle.record_failure(throttle.LOGIN, key)
    res = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW})
    assert res.status_code == 429
    assert MemberApiToken.objects.count() == 0


@_PUBLIC_HOST
def test_ip_lock_blocks_with_429(db, http_client):
    _member()
    for _ in range(50):
        throttle.record_failure(throttle.LOGIN_IP, "127.0.0.1")
    res = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW})
    assert res.status_code == 429


# ---- TOTP 2FA ----


def _totp_member():
    secret = pyotp.random_base32()
    return _member(
        two_factor_method=TwoFactorMethod.TOTP,
        totp_secret=secret,
        totp_confirmed_at=timezone.now(),
    )


@_PUBLIC_HOST
def test_totp_login_is_two_step(db, http_client):
    m = _totp_member()
    first = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "2fa_required"
    assert body["method"] == "totp"
    assert body["token"] == ""  # 第2要素前にトークンを出さない
    assert MemberApiToken.objects.count() == 0

    code = pyotp.TOTP(m.totp_secret).now()
    second = _post(http_client, _LOGIN_2FA, {"challenge_id": body["challenge_id"], "code": code})
    assert second.status_code == 200
    assert second.json()["status"] == "ok"
    assert second.json()["token"]


@_PUBLIC_HOST
def test_totp_wrong_code_is_401_and_no_token(db, http_client):
    _totp_member()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    res = _post(http_client, _LOGIN_2FA, {"challenge_id": cid, "code": "000000"})
    assert res.status_code == 401
    assert MemberApiToken.objects.count() == 0


@_PUBLIC_HOST
def test_totp_replay_is_rejected(db, http_client):
    """同じコードでの2回目は拒否 (HTML 版と同じ totp_last_step 判定)。"""
    m = _totp_member()
    code = pyotp.TOTP(m.totp_secret).now()

    cid1 = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    assert _post(http_client, _LOGIN_2FA, {"challenge_id": cid1, "code": code}).status_code == 200

    cid2 = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    assert _post(http_client, _LOGIN_2FA, {"challenge_id": cid2, "code": code}).status_code == 401
    assert MemberApiToken.objects.count() == 1


@_PUBLIC_HOST
def test_totp_second_factor_is_throttled(db, http_client):
    m = _totp_member()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    for _ in range(20):
        throttle.record_failure(throttle.TOTP, str(m.pk))
    res = _post(
        http_client, _LOGIN_2FA, {"challenge_id": cid, "code": pyotp.TOTP(m.totp_secret).now()}
    )
    assert res.status_code == 429
    assert MemberApiToken.objects.count() == 0


# ---- challenge の性質 ----


@_PUBLIC_HOST
def test_challenge_is_single_use(db, http_client):
    m = _totp_member()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    code = pyotp.TOTP(m.totp_secret).now()
    assert _post(http_client, _LOGIN_2FA, {"challenge_id": cid, "code": code}).status_code == 200
    # 消費済みなので同じ challenge は再利用できない (コードの正誤に関わらず)。
    assert _post(http_client, _LOGIN_2FA, {"challenge_id": cid, "code": code}).status_code == 401
    assert MemberApiToken.objects.count() == 1


@_PUBLIC_HOST
def test_expired_challenge_is_rejected(db, http_client):
    m = _totp_member()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    MemberLoginChallenge.objects.filter(challenge_id=cid).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    res = _post(
        http_client, _LOGIN_2FA, {"challenge_id": cid, "code": pyotp.TOTP(m.totp_secret).now()}
    )
    assert res.status_code == 401


@_PUBLIC_HOST
def test_unknown_challenge_is_rejected(db, http_client):
    _totp_member()
    res = _post(
        http_client,
        _LOGIN_2FA,
        {"challenge_id": "00000000-0000-0000-0000-000000000000", "code": "123456"},
    )
    assert res.status_code == 401


# ---- メール 2FA ----


@_PUBLIC_HOST
@_LOCMEM
def test_email_2fa_login_flow(db, http_client):
    _member(two_factor_method=TwoFactorMethod.EMAIL)
    mail.outbox.clear()
    body = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()
    assert body["status"] == "2fa_required"
    assert body["method"] == "email"
    assert len(mail.outbox) == 1

    code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
    res = _post(http_client, _LOGIN_2FA, {"challenge_id": body["challenge_id"], "code": code})
    assert res.status_code == 200
    assert res.json()["token"]


@_PUBLIC_HOST
@_LOCMEM
def test_email_2fa_resend(db, http_client):
    _member(two_factor_method=TwoFactorMethod.EMAIL)
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    mail.outbox.clear()
    res = _post(http_client, _RESEND, {"challenge_id": cid})
    # 直近送信済みならスロットルされる。どちらでもコードは失われない。
    assert res.status_code in (200, 429)


@_PUBLIC_HOST
def test_resend_rejects_totp_challenge(db, http_client):
    """TOTP にメール再送は無い (誤って送信経路を開かない)。"""
    _totp_member()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    assert _post(http_client, _RESEND, {"challenge_id": cid}).status_code == 401


# ---- トークンのライフサイクル ----


@_PUBLIC_HOST
def test_logout_revokes_only_that_token(db, http_client):
    _member()
    a = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]
    b = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]

    assert http_client.post(_LOGOUT, **_auth(a)).status_code == 200
    assert http_client.get(_ME, **_auth(a)).status_code == 401
    assert http_client.get(_ME, **_auth(b)).status_code == 200  # 他端末は生きている


@_PUBLIC_HOST
def test_expired_token_is_rejected(db, http_client):
    _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]
    MemberApiToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert http_client.get(_ME, **_auth(token)).status_code == 401


@_PUBLIC_HOST
def test_token_of_deactivated_member_is_rejected(db, http_client):
    m = _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]
    Member.objects.filter(pk=m.pk).update(is_active=False)
    assert http_client.get(_ME, **_auth(token)).status_code == 401


@_PUBLIC_HOST
def test_garbage_token_is_rejected(db, http_client):
    _member()
    assert http_client.get(_ME, **_auth("not-a-real-token")).status_code == 401


@_PUBLIC_HOST
def test_session_wins_over_bearer(db, http_client):
    """ブラウザのリクエストに紛れた Authorization ヘッダで会員が入れ替わらないこと。"""
    _member(email="a@example.com")
    _member(email="b@example.com")
    token_b = _post(http_client, _LOGIN, {"email": "b@example.com", "password": _PW}).json()[
        "token"
    ]
    http_client.post("/members/login/", {"email": "a@example.com", "password": _PW})

    res = http_client.get(_ME, **_auth(token_b))
    assert res.status_code == 200
    assert res.json()["email"] == "a@example.com"


@_PUBLIC_HOST
def test_totp_last_step_is_shared_with_html_login(db, http_client):
    """アプリで使ったコードは HTML ログインでも再利用できないこと (経路をまたぐリプレイ防止)。"""
    m = _totp_member()
    code = pyotp.TOTP(m.totp_secret).now()
    cid = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()[
        "challenge_id"
    ]
    assert _post(http_client, _LOGIN_2FA, {"challenge_id": cid, "code": code}).status_code == 200

    m.refresh_from_db()
    assert m.totp_last_step == totp.matched_step(m.totp_secret, code)


# ---- セキュリティレビュー由来の回帰テスト ----


@_PUBLIC_HOST
def test_bearer_header_cannot_skip_csrf_for_session_request(db, channel):
    """session で認証されている非 GET は、Authorization ヘッダを添えても CSRF を免れない。

    ninja は最初に成功した auth callback で連鎖を打ち切るため、MemberTokenAuth が session 由来の
    会員を返してしまうと MemberAuth の CSRF 検査ごと飛ばせてしまう。HttpBearer はスキーム名しか
    見ない (トークンの中身は何でもよい) ので、これは実質 CSRF 無効化に等しい。
    """
    from django.test import Client

    _member()
    # enforce_csrf_checks=True の Client でないと本番の CSRF 経路を再現できない
    # (既定の http_client は CSRF を無効化しているため、この不具合を素通りさせてしまう)。
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.post("/members/login/", {"email": "m@example.com", "password": _PW})

    res = csrf_client.post(
        f"/api/v1/channels/{channel.slug}/pin",
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer anything-at-all",
    )
    assert res.status_code == 403


@_PUBLIC_HOST
def test_password_change_revokes_all_tokens(db, http_client):
    """パスワード変更で他端末のアプリログインが切れること。

    トークンの確認は session を持たない別クライアントで行う。パスワードを変えた本人の
    session は cycle_key で生き続けるため、同じクライアントで叩くと session 側で認証が通り、
    トークンが失効したかどうかを確かめられない。
    """
    from django.test import Client

    m = _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]

    app = Client()  # アプリ相当。cookie を持たない
    assert app.get(_ME, **_auth(token)).status_code == 200

    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    new_pw = "Zx7#qwer9012"  # pragma: allowlist secret - test only
    res = http_client.post(
        "/members/password/",
        {"current_password": _PW, "new_password": new_pw, "new_password_confirm": new_pw},
    )
    assert res.status_code == 302
    m.refresh_from_db()
    assert m.check_password(new_pw), "前提: パスワード変更が成功していること"

    assert app.get(_ME, **_auth(token)).status_code == 401


@_PUBLIC_HOST
@_LOCMEM
def test_password_reset_revokes_all_tokens(db, http_client):
    """リセットは旧パスワードを知らずに到達できるため、乗っ取り時の締め出しに効く必要がある。"""

    m = _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]

    mail.outbox.clear()
    http_client.post("/members/password/reset/", {"email": "m@example.com"})
    code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)

    new_pw = "Zx7#qwer9012"  # pragma: allowlist secret - test only
    http_client.post(
        "/members/password/reset/confirm/",
        {
            "email": "m@example.com",
            "code": code,
            "new_password": new_pw,
            "new_password_confirm": new_pw,
        },
    )
    m.refresh_from_db()
    assert m.check_password(new_pw), "前提: リセットが成功していること"
    assert http_client.get(_ME, **_auth(token)).status_code == 401


@_PUBLIC_HOST
def test_last_used_at_is_not_written_on_every_request(db, http_client):
    """閲覧と同じ量の UPDATE を出さないこと (粗い活動把握が用途)。"""
    _member()
    token = _post(http_client, _LOGIN, {"email": "m@example.com", "password": _PW}).json()["token"]

    http_client.get(_ME, **_auth(token))
    first = MemberApiToken.objects.get().last_used_at
    assert first is not None

    http_client.get(_ME, **_auth(token))
    assert MemberApiToken.objects.get().last_used_at == first  # 間隔内は書き換えない

    MemberApiToken.objects.update(last_used_at=timezone.now() - timedelta(minutes=10))
    http_client.get(_ME, **_auth(token))
    assert MemberApiToken.objects.get().last_used_at > first  # 間隔を超えたら更新する
