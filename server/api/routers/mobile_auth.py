# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ネイティブアプリ向け トークン認証 API (#MOBILE-01)。

Web の HTML ログイン (members.views.login_view) は session cookie + CSRF + リダイレクト連鎖の
ままで変更しない。アプリは cookie jar と HTML リダイレクトのどちらも扱いにくいため、同じ
認証ロジックを JSON の多段 API として出し直す。

第2要素 (2FA) は HTML 版が session に pending を持つのに対し、こちらは challenge_id を
クライアントへ返して往復させる。第2要素を通すまでトークンを発行しない点は同じ
(中途半端な状態でログイン扱いにしない)。

**スロットル・監査ログ・タイミング平準化・TOTP リプレイ防止は HTML 版と同一の仕組みを
そのまま使う**。ここが独自実装になると、片方だけ緩い迂回路ができてしまう。
"""

from __future__ import annotations

import logging

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from api.auth import member_any_auth
from api.schemas import (
    MobileLogin2faIn,
    MobileLoginIn,
    MobileLoginOut,
    MobileMemberOut,
    MobileResendIn,
    OkOut,
)
from core.security_log import emit as security_emit
from members import api_tokens, codes, throttle, totp
from members.forms import normalize_email
from members.middleware import bearer_token
from members.models import CodePurpose, Member, TwoFactorMethod

logger = logging.getLogger(__name__)

router = Router(tags=["mobile-auth"])

_THROTTLED = "試行回数が多すぎます。しばらくしてからお試しください。"
_BAD_CREDENTIALS = "メールアドレスまたはパスワードが違います。"
_BAD_CHALLENGE = "ログインをやり直してください。"
_BAD_CODE = "コードが正しくないか、有効期限が切れています。"


def _member_payload(member) -> dict:
    return {
        "id": member.pk,
        "nickname": member.nickname,
        "email": member.email,
        "is_verified": member.is_verified,
    }


def _issue(member, device_label: str) -> dict:
    raw = api_tokens.issue(member, device_label)
    return {
        "status": "ok",
        "token": raw,
        "member": _member_payload(member),
    }


@router.post("/auth/login", response=MobileLoginOut, auth=None)
def login(request: HttpRequest, payload: MobileLoginIn):
    """第1要素。2FA 未設定なら即トークン、設定済みなら challenge を返す。

    2FA が要る場合も HTTP は 200 で返す (第1要素は成功しているため)。クライアントは status で
    分岐する。認証失敗そのものは 401。
    """
    ip = throttle.client_ip(request)
    # IP ロックは email 取得前に判定できる (HTML 版と同じ順序)。
    if throttle.is_locked(throttle.LOGIN_IP, ip):
        security_emit("auth.member.login", outcome="blocked", request=request, reason="ip_locked")
        raise HttpError(429, _THROTTLED)

    email = normalize_email(payload.email or "")
    email_key = throttle.hash_email(email)
    if throttle.is_locked(throttle.LOGIN, email_key):
        security_emit(
            "auth.member.login",
            outcome="blocked",
            request=request,
            reason="email_locked",
            email_hash=email_key,
        )
        raise HttpError(429, _THROTTLED)

    password = payload.password or ""
    member = Member.objects.filter(email__iexact=email, is_active=True).first()
    if member is None or not member.check_password(password):
        if member is None:
            # 存在しなくてもハッシュ計算してタイミング差/列挙を平す (HTML 版と同手法)。
            Member().set_password(password)
        throttle.record_failure(throttle.LOGIN, email_key)
        throttle.record_failure(throttle.LOGIN_IP, ip)
        security_emit("auth.member.login", outcome="fail", request=request, email_hash=email_key)
        raise HttpError(401, _BAD_CREDENTIALS)

    # 成功: email 側カウンタは消す。IP 側は同一 IP の攻撃者を守らないため残す (HTML 版と同じ)。
    throttle.clear(throttle.LOGIN, email_key)
    security_emit(
        "auth.member.login",
        outcome="success",
        request=request,
        member_id=member.pk,
        email_hash=email_key,
        via="token",
    )

    device_label = payload.device_label or ""
    method = member.two_factor_method
    if method == TwoFactorMethod.TOTP and member.totp_confirmed_at:
        challenge = api_tokens.start_challenge(member, "totp", device_label)
        return {
            "status": "2fa_required",
            "method": "totp",
            "challenge_id": str(challenge.challenge_id),
        }
    if method == TwoFactorMethod.EMAIL and member.email_verified_at:
        try:
            codes.send_code(member, CodePurpose.LOGIN_2FA)
        except codes.SendThrottledError:
            pass  # 直近に送信済 → 既存コードで継続
        except Exception:
            logger.exception("mobile login 2fa email send failed: member=%s", member.pk)
        challenge = api_tokens.start_challenge(member, "email", device_label)
        return {
            "status": "2fa_required",
            "method": "email",
            "challenge_id": str(challenge.challenge_id),
        }

    return _issue(member, device_label)


@router.post("/auth/login/2fa", response=MobileLoginOut, auth=None)
def login_2fa(request: HttpRequest, payload: MobileLogin2faIn):
    """第2要素。challenge_id + コードでトークンを受け取る。"""
    challenge = api_tokens.get_challenge(payload.challenge_id)
    if challenge is None:
        raise HttpError(401, _BAD_CHALLENGE)

    member = challenge.member
    if not member.is_active:
        raise HttpError(401, _BAD_CHALLENGE)

    ip = throttle.client_ip(request)
    mkey = str(member.pk)
    # 第2要素にも Cookie 非依存の試行上限を掛ける (HTML 版と同じ scope を共有するので、
    # アプリ経由の総当たりは web 側のロックにも反映される)。
    if throttle.is_locked(throttle.TOTP, mkey) or throttle.is_locked(throttle.TOTP_IP, ip):
        security_emit(
            "auth.member.2fa",
            outcome="blocked",
            request=request,
            method=challenge.method,
            member_id=member.pk,
        )
        raise HttpError(429, _THROTTLED)

    code = payload.code or ""
    if challenge.method == "totp":
        step = totp.matched_step(member.totp_secret, code)
        # step が直近使用ステップ以下ならリプレイ (または時刻ずれ窓での再送) として拒否。
        ok = step is not None and (member.totp_last_step is None or step > member.totp_last_step)
        if ok:
            member.totp_last_step = step
            member.save(update_fields=["totp_last_step", "updated_at"])
    else:
        ok = codes.verify_code(member, CodePurpose.LOGIN_2FA, code)

    if not ok:
        throttle.record_failure(throttle.TOTP, mkey)
        throttle.record_failure(throttle.TOTP_IP, ip)
        security_emit(
            "auth.member.2fa",
            outcome="fail",
            request=request,
            method=challenge.method,
            member_id=member.pk,
        )
        raise HttpError(401, _BAD_CODE)

    # 単回使用。並行リクエストのどちらか一方しか通らない (トークンの二重発行を防ぐ)。
    if not api_tokens.consume_challenge(challenge):
        raise HttpError(401, _BAD_CHALLENGE)

    throttle.clear(throttle.TOTP, mkey)
    security_emit(
        "auth.member.2fa",
        outcome="success",
        request=request,
        method=challenge.method,
        member_id=member.pk,
        via="token",
    )
    return _issue(member, challenge.device_label)


@router.post("/auth/login/2fa/resend", response=OkOut, auth=None)
def login_2fa_resend(request: HttpRequest, payload: MobileResendIn):
    """メール 2FA のコード再送。TOTP の challenge には無効。"""
    challenge = api_tokens.get_challenge(payload.challenge_id)
    if challenge is None or challenge.method != "email":
        raise HttpError(401, _BAD_CHALLENGE)
    try:
        codes.send_code(challenge.member, CodePurpose.LOGIN_2FA)
    except codes.SendThrottledError as e:
        raise HttpError(429, f"{e.retry_after}秒後に再度お試しください。") from e
    except Exception:
        logger.exception("mobile login 2fa resend failed: member=%s", challenge.member_id)
        raise HttpError(502, "メール送信に失敗しました。") from None
    return {"ok": True}


@router.post("/auth/logout", response=OkOut, auth=member_any_auth)
def logout(request: HttpRequest):
    """このトークン 1 本だけ失効させる (他端末のログインは維持)。"""
    api_tokens.revoke(bearer_token(request))
    return {"ok": True}


@router.get("/auth/me", response=MobileMemberOut, auth=member_any_auth)
def me(request: HttpRequest):
    member = getattr(request, "member", None)
    assert member is not None  # auth が非ログインを 401 で弾くため到達時は非 None
    return _member_payload(member)
