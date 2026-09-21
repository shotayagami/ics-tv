# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員のメール確認コード (発行/送信/検証)。

6桁コードは make_password でハッシュ保管 (平文は DB に残さない・メール本文にのみ出る)。
発行はクールダウン+時間内上限でレート制限。検証は最新の有効コードと照合し、成功で consume、
失敗で attempts++ (上限到達でコードは死ぬ)。
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from members.models import CodePurpose, MemberEmailCode

# 用途別のメール件名/本文ラベル (1 テンプレを使い回す)
_PURPOSE_LABEL: dict[str, str] = {
    CodePurpose.EMAIL_VERIFY: "確認コード",
    CodePurpose.LOGIN_2FA: "ログイン確認コード",
    CodePurpose.PASSWORD_RESET: "パスワード再設定コード",
    CodePurpose.EMAIL_CHANGE: "メールアドレス確認コード",
}


class SendThrottledError(Exception):
    """再送がクールダウン中 / 時間内上限超過。"""

    def __init__(self, retry_after: float):
        self.retry_after = max(1, int(retry_after))
        super().__init__("送信制限中")


def _ttl() -> int:
    return int(getattr(settings, "ICSTV_MEMBER_CODE_TTL", 600))


def _max_attempts() -> int:
    return int(getattr(settings, "ICSTV_MEMBER_CODE_MAX_ATTEMPTS", 5))


def _cooldown() -> int:
    return int(getattr(settings, "ICSTV_MEMBER_CODE_SEND_COOLDOWN", 60))


def _max_per_hour() -> int:
    return int(getattr(settings, "ICSTV_MEMBER_CODE_SEND_MAX_PER_HOUR", 8))


def issue_code(member, purpose: str) -> str:
    """新しいコードを発行し平文を返す (送信に使う / テストから取得)。レート制限あり。"""
    now = timezone.now()
    recent = MemberEmailCode.objects.filter(member=member, purpose=purpose).order_by("-created_at")
    last = recent.first()
    if last is not None:
        elapsed = (now - last.created_at).total_seconds()
        if elapsed < _cooldown():
            raise SendThrottledError(_cooldown() - elapsed)
    if recent.filter(created_at__gte=now - timedelta(hours=1)).count() >= _max_per_hour():
        raise SendThrottledError(3600)
    code = f"{secrets.randbelow(10**6):06d}"
    MemberEmailCode.objects.create(
        member=member,
        purpose=purpose,
        code_hash=make_password(code),
        expires_at=now + timedelta(seconds=_ttl()),
    )
    return code


def send_code(member, purpose: str, to: str | None = None) -> None:
    """コードを発行しメール送信する。

    to を指定すると別アドレス宛に送る (メール変更で新アドレスへ送る用途)。既定は member.email。
    レート制限超過は SendThrottledError、送信失敗は例外伝播。
    """
    code = issue_code(member, purpose)
    label = _PURPOSE_LABEL.get(purpose, "確認コード")
    body = render_to_string(
        "members/email/verify_code.txt",
        {"code": code, "member": member, "ttl_min": max(1, _ttl() // 60), "label": label},
    )
    send_mail(
        f"【ICS-TV】{label}",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [to or member.email],
        fail_silently=False,
    )


def verify_code(member, purpose: str, code: str) -> bool:
    """最新の有効コードと照合。成功で consume(単回)、失敗で attempts++。"""
    code = (code or "").strip()
    now = timezone.now()
    rec = (
        MemberEmailCode.objects.filter(
            member=member, purpose=purpose, consumed_at__isnull=True, expires_at__gt=now
        )
        .order_by("-created_at")
        .first()
    )
    if rec is None or rec.attempts >= _max_attempts():
        return False
    if code and check_password(code, rec.code_hash):
        rec.consumed_at = now
        rec.save(update_fields=["consumed_at"])
        return True
    rec.attempts += 1
    rec.save(update_fields=["attempts"])
    return False
