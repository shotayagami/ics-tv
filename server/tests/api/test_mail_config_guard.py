# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""メール送信元 (DEFAULT_FROM_EMAIL) の fail-closed 設定ガードの単体テスト (config.checks)。"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.checks import check_mail_config, enforce_mail_config

_SMTP = "django.core.mail.backends.smtp.EmailBackend"
_LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
_GOOD = {
    "email_backend": _SMTP,
    "default_from_email": "ICS-TV <no-reply@example.invalid>",
}


def test_healthy_config_has_no_problems():
    assert check_mail_config(**_GOOD) == []


def test_empty_sender_with_smtp_flagged():
    p = check_mail_config(**{**_GOOD, "default_from_email": ""})
    assert len(p) == 1 and "DJANGO_DEFAULT_FROM_EMAIL" in p[0]


def test_blank_sender_with_smtp_flagged():
    p = check_mail_config(**{**_GOOD, "default_from_email": " \t "})
    assert len(p) == 1 and "DJANGO_DEFAULT_FROM_EMAIL" in p[0]


@pytest.mark.parametrize(
    "backend",
    [
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    ],
)
def test_non_delivering_backend_with_empty_sender_not_flagged(backend):
    """配送しない backend は dev/test/CI の既定なので、送信元が空でも不備にしない。"""
    assert check_mail_config(email_backend=backend, default_from_email="") == []


def test_enforce_raises_on_empty_sender_with_smtp():
    with pytest.raises(ImproperlyConfigured):
        enforce_mail_config(email_backend=_SMTP, default_from_email="")


def test_enforce_passes_on_healthy():
    enforce_mail_config(**_GOOD)  # 例外が出なければ OK


def test_enforce_passes_on_non_delivering_backend_with_empty_sender():
    enforce_mail_config(email_backend=_LOCMEM, default_from_email="")  # 例外が出なければ OK
