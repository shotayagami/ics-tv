# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員向け通知メール (リマインド等)。

codes.py の確認コード送信とは別系統 (汎用の本文メール)。Mailgun SMTP (本番) / console (dev)
/ locmem (test) は settings.EMAIL_BACKEND で切替。呼び出し側で is_verified/期限を確認済み前提。
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone


def _base_url() -> str:
    return (getattr(settings, "ICSTV_PUBLIC_BASE_URL", "") or "").rstrip("/")


def send_program_reminder(member, program) -> None:
    """番組開始前リマインドメール (#EPG-03)。"""
    base = _base_url()
    body = render_to_string(
        "members/email/program_reminder.txt",
        {
            "member": member,
            "program": program,
            "start_local": timezone.localtime(program.start_at),
            "channel_url": f"{base}/ch/{program.channel.slug}/",
            "program_url": f"{base}/program/{program.id}/",
        },
    )
    send_mail(
        f"まもなく放送: {program.title} — ICS-TV",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [member.email],
        fail_silently=False,
    )
