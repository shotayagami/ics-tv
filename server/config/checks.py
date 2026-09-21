# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""本番の fail-closed 設定ガード (#sec M-5 / M-6 / I-4)。

SECRET_KEY / at-rest 暗号鍵 / DB 資格情報が「未設定 = git 追跡の既知既定値・平文フォールバック・
弱いデフォルト資格情報」のまま本番起動するのを防ぐ。settings 末尾で ICSTV_REQUIRE_SECRETS が真の
時だけ検証し、不備があれば ImproperlyConfigured で起動そのものを止める (gunicorn/celery/manage.py
すべて)。テスト/ローカル/CI は既定 False で不介入 (これらは既定値のまま DEBUG=False で走るため)。

deploy は base configmap で ICSTV_REQUIRE_SECRETS=true を設定する。実運用では 3 値とも sealed
secret 由来の実値が入るため常に pass し、env 名タイポ・反映漏れ・新ノードでの未注入だけを弾く。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

# settings.py の各既定値 (= 未設定を意味する危険な値)。ここを唯一の真実源にする。
INSECURE_SECRET_KEY = "dev-insecure-change-me"  # pragma: allowlist secret
INSECURE_DATABASE_URL = "postgres://icstv:icstv@localhost:5432/icstv"  # pragma: allowlist secret
MIN_DISTRIBUTION_SECRET_LENGTH = 32
MAX_HLS_TOKEN_TTL_SEC = 86400


def parse_distribution_mode(value: object) -> bool:
    """Django/Worker共通の配布mode文字列規約を判定する。"""
    normalized = str("" if value is None else value).strip().casefold()
    if normalized in {"true", "on", "ok", "y", "yes", "1"}:
        return True
    digits = normalized[1:] if normalized[:1] in {"+", "-"} else normalized
    return (
        bool(digits)
        and all("0" <= digit <= "9" for digit in digits)
        and any(digit != "0" for digit in digits)
    )


def _database_credentials(database_url: str) -> tuple[str | None, str | None, bool]:
    """URL userinfoをpercent-decodeし、解析可否とともに返す。値は診断文へ含めない。"""
    try:
        parsed = urlsplit(database_url)
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
    except (AttributeError, TypeError, ValueError):
        return None, None, False
    return username, password, True


INSECURE_DATABASE_USER, INSECURE_DATABASE_PASSWORD, _ = _database_credentials(INSECURE_DATABASE_URL)


def check_secure_config(
    *, secret_key: str, field_encryption_key: str, database_url: str
) -> list[str]:
    """本番不備メッセージの一覧を返す (空リスト = 健全)。副作用なし・単体テスト可能。"""
    problems: list[str] = []
    if not secret_key or secret_key == INSECURE_SECRET_KEY:
        problems.append(
            "DJANGO_SECRET_KEY が未設定 (git 追跡の既知既定値)。"
            "session cookie / パスワードリセットトークンを偽造され会員・管理者になりすまされます。"
        )
    if not field_encryption_key:
        problems.append(
            "ICSTV_FIELD_ENCRYPTION_KEY が未設定。TOTP 秘密 / agent_token / YouTube OAuth を"
            "平文で DB 保管し、DB ダンプ流出時に機密が露出します。"
        )
    if not database_url or database_url == INSECURE_DATABASE_URL:
        problems.append("DATABASE_URL が未設定 (弱い既定資格情報 icstv:icstv@localhost)。")
    return problems


def enforce_secure_config(*, secret_key: str, field_encryption_key: str, database_url: str) -> None:
    """不備があれば ImproperlyConfigured を送出して起動を止める。"""
    problems = check_secure_config(
        secret_key=secret_key,
        field_encryption_key=field_encryption_key,
        database_url=database_url,
    )
    if problems:
        raise ImproperlyConfigured(
            "本番セキュリティ設定の不備を検出しました (ICSTV_REQUIRE_SECRETS=true)。"
            "sealed secret の注入を確認してください:\n- " + "\n- ".join(problems)
        )


def check_distribution_config(
    *,
    debug: bool,
    secret_key: str,
    field_encryption_key: str,
    database_url: str,
    hls_signing_key: str,
    hls_token_ttl_sec: int,
    allowed_hosts: list[str],
) -> list[str]:
    """明示的な配布モードで拒否する設定不備を、値を含めずに返す。

    文字数の下限は、既知の短い値や設定漏れを検出する最低条件にすぎず、鍵の
    エントロピーを保証しない。鍵は安全な乱数生成器で個別に生成・保管する必要がある。
    """
    problems: list[str] = []
    if debug:
        problems.append("DJANGO_DEBUG は配布モードで false が必要です。")
    if not secret_key or secret_key == INSECURE_SECRET_KEY:
        problems.append("DJANGO_SECRET_KEY が未設定または既知の開発用既定値です。")
    elif len(secret_key) < MIN_DISTRIBUTION_SECRET_LENGTH:
        problems.append(
            f"DJANGO_SECRET_KEY は配布モードで {MIN_DISTRIBUTION_SECRET_LENGTH} 文字以上が必要です。"
        )

    if not field_encryption_key:
        problems.append("ICSTV_FIELD_ENCRYPTION_KEY が未設定です。")
    else:
        try:
            Fernet(field_encryption_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError, TypeError):
            problems.append("ICSTV_FIELD_ENCRYPTION_KEY は有効なFernet鍵ではありません。")

    database_user, database_password, database_url_valid = _database_credentials(database_url)
    if database_url and not database_url_valid:
        problems.append("DATABASE_URL の形式を解析できません。")
    elif not database_url or not database_user or not database_password:
        problems.append("DATABASE_URL に明示的なDBユーザーとパスワードが必要です。")
    elif (
        INSECURE_DATABASE_USER is not None
        and INSECURE_DATABASE_PASSWORD is not None
        and database_user.casefold() == INSECURE_DATABASE_USER.casefold()
        and database_password == INSECURE_DATABASE_PASSWORD
    ):
        problems.append("DATABASE_URL が既知の弱い既定DB資格情報を使用しています。")

    if not hls_signing_key:
        problems.append("ICSTV_HLS_SIGNING_KEY は配布モードで必須です。")
    elif hls_signing_key == secret_key:
        problems.append("ICSTV_HLS_SIGNING_KEY はDJANGO_SECRET_KEYと分離する必要があります。")
    elif len(hls_signing_key) < MIN_DISTRIBUTION_SECRET_LENGTH:
        problems.append(
            f"ICSTV_HLS_SIGNING_KEY は配布モードで {MIN_DISTRIBUTION_SECRET_LENGTH} 文字以上が必要です。"
        )

    if not isinstance(hls_token_ttl_sec, int) or isinstance(hls_token_ttl_sec, bool):
        problems.append("ICSTV_HLS_TOKEN_TTL_SEC は整数で指定する必要があります。")
    elif not 1 <= hls_token_ttl_sec <= MAX_HLS_TOKEN_TTL_SEC:
        problems.append(
            f"ICSTV_HLS_TOKEN_TTL_SEC は1秒以上{MAX_HLS_TOKEN_TTL_SEC}秒以下が必要です。"
        )

    if not isinstance(allowed_hosts, list) or any(
        not isinstance(host, str) for host in allowed_hosts
    ):
        problems.append("DJANGO_ALLOWED_HOSTS は文字列のリストで指定する必要があります。")
    elif not allowed_hosts or any(not host.strip() for host in allowed_hosts):
        problems.append("DJANGO_ALLOWED_HOSTS は配布モードで明示する必要があります。")
    elif any("*" in host or host.strip().startswith(".") for host in allowed_hosts):
        problems.append("DJANGO_ALLOWED_HOSTS は配布モードでワイルドカードを使用できません。")
    return problems


def enforce_distribution_config(
    *,
    debug: bool,
    secret_key: str,
    field_encryption_key: str,
    database_url: str,
    hls_signing_key: str,
    hls_token_ttl_sec: int,
    allowed_hosts: list[str],
) -> None:
    """配布モードの設定不備があれば、秘密値を表示せず起動を止める。"""
    problems = check_distribution_config(
        debug=debug,
        secret_key=secret_key,
        field_encryption_key=field_encryption_key,
        database_url=database_url,
        hls_signing_key=hls_signing_key,
        hls_token_ttl_sec=hls_token_ttl_sec,
        allowed_hosts=allowed_hosts,
    )
    if problems:
        raise ImproperlyConfigured(
            "配布モードのセキュリティ設定に不備があります "
            "(ICSTV_DISTRIBUTION_MODE=true):\n- " + "\n- ".join(problems)
        )


def check_operator_config(
    *,
    legal_name: str,
    representative_name: str,
    address: str,
    phone: str,
    hide_contact_details: bool,
    contact_email: str,
) -> list[str]:
    """運営者表示 (特定商取引法11条相当) の不備メッセージの一覧を返す (空リスト = 健全)。

    fanclub.models.Creator の同名フィールドと同じ語彙。hide_contact_details=True の間は
    公開画面で address/phone を表示しない運用 (fc_tokushoho.html と同じ) のため、
    その間は address/phone を必須にしない。
    """
    problems: list[str] = []
    if not legal_name:
        problems.append("ICSTV_OPERATOR_LEGAL_NAME が未設定です。")
    if not representative_name:
        problems.append("ICSTV_OPERATOR_REPRESENTATIVE_NAME が未設定です。")
    if not hide_contact_details and not (address and phone):
        problems.append(
            "ICSTV_OPERATOR_HIDE_CONTACT_DETAILS=false のときは "
            "ICSTV_OPERATOR_ADDRESS と ICSTV_OPERATOR_PHONE の両方が必要です。"
        )
    if not contact_email:
        problems.append("ICSTV_OPERATOR_CONTACT_EMAIL が未設定です。")
    return problems


def enforce_operator_config(
    *,
    legal_name: str,
    representative_name: str,
    address: str,
    phone: str,
    hide_contact_details: bool,
    contact_email: str,
) -> None:
    """運営者表示の不備があれば ImproperlyConfigured を送出して起動を止める。"""
    problems = check_operator_config(
        legal_name=legal_name,
        representative_name=representative_name,
        address=address,
        phone=phone,
        hide_contact_details=hide_contact_details,
        contact_email=contact_email,
    )
    if problems:
        raise ImproperlyConfigured(
            "運営者表示 (特定商取引法に基づく表記) の設定不備を検出しました "
            "(ICSTV_REQUIRE_OPERATOR_INFO=true)。値を設定してください:\n- " + "\n- ".join(problems)
        )


# Django 同梱の「実際には配送しない」メール backend。dev (console) / test (locmem) / CI の既定で、
# 送信元 (DEFAULT_FROM_EMAIL) が空でも実害がないため、メール送信元ガードの対象外にする。
NON_DELIVERING_EMAIL_BACKENDS = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    }
)


def check_mail_config(*, email_backend: str, default_from_email: str) -> list[str]:
    """メール送信元の不備メッセージの一覧を返す (空リスト = 健全)。副作用なし・単体テスト可能。

    配送する backend (smtp など、NON_DELIVERING_EMAIL_BACKENDS 以外) で、送信元が空または
    空白のみのときだけ不備とする。settings の DEFAULT_FROM_EMAIL は既定が空なので、env の
    DJANGO_DEFAULT_FROM_EMAIL の設定漏れもここで検出される。
    """
    problems: list[str] = []
    if email_backend not in NON_DELIVERING_EMAIL_BACKENDS and not default_from_email.strip():
        problems.append(
            f"DJANGO_EMAIL_BACKEND={email_backend} は実際に配送する backend ですが、"
            "DJANGO_DEFAULT_FROM_EMAIL が未設定 (空または空白のみ) です。"
        )
    return problems


def enforce_mail_config(*, email_backend: str, default_from_email: str) -> None:
    """メール送信元の不備があれば ImproperlyConfigured を送出して起動を止める。"""
    problems = check_mail_config(
        email_backend=email_backend,
        default_from_email=default_from_email,
    )
    if problems:
        raise ImproperlyConfigured(
            "メール送信元の設定不備を検出しました。DJANGO_DEFAULT_FROM_EMAIL を設定するか、"
            "配送しない backend を指定してください:\n- " + "\n- ".join(problems)
        )
