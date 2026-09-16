# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""明示配布モードのfail-closed設定ガード。DB・外部接続は使用しない。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

import config.checks as checks
from config.checks import (
    check_distribution_config,
    enforce_distribution_config,
    parse_distribution_mode,
)

FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="  # pragma: allowlist secret
SECRET_KEY = "django-synthetic-secret-key-000001"  # pragma: allowlist secret
HLS_KEY = "hls-synthetic-signing-key-0000001"  # pragma: allowlist secret
# 下の parametrize で使う不正な接続文字列。定数へ出すのは、行に注記を付けたまま
# 1 行に収めるため (dict の中に置くと長さ上限を超え、整形で注記が値の行から離れる)。
REMOTE_DB_URL = "postgres://icstv:icstv@remote.invalid:5432/runtime"  # pragma: allowlist secret
ENCODED_DB_URL = "postgres://%69CSTV:ic%73tv@other.invalid:5432/runtime"  # pragma: allowlist secret
MALFORMED_DB_URL = "postgres://user:pass@[invalid/runtime"  # pragma: allowlist secret

GOOD = {
    "debug": False,
    "secret_key": SECRET_KEY,
    "field_encryption_key": FERNET_KEY,
    "database_url": "postgres://runtime-user:synthetic-pass@db.invalid:5432/runtime",  # pragma: allowlist secret
    "hls_signing_key": HLS_KEY,
    "hls_token_ttl_sec": 3600,
    "allowed_hosts": ["tv.example.invalid"],
}


def test_distribution_config_accepts_explicit_synthetic_values():
    assert check_distribution_config(**GOOD) == []
    enforce_distribution_config(**GOOD)


@pytest.mark.parametrize(
    "value",
    [
        "true",
        "TRUE",
        "on",
        "OK",
        "y",
        "YES",
        "1",
        "2",
        "007",
        "+1",
        "-1",
        "  +2  ",
        "0" * 4500 + "1",
    ],
)
def test_distribution_mode_parser_accepts_shared_true_syntax(value):
    assert parse_distribution_mode(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "false",
        "t",
        "0",
        "+0",
        "-000",
        "1_0",
        "１",
        "١",
        "1.0",
        "0x1",
        "1e3",
        "+",
        "--1",
        "1 2",
        "   ",
    ],
)
def test_distribution_mode_parser_rejects_other_syntax(value):
    assert parse_distribution_mode(value) is False


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"debug": True}, "DJANGO_DEBUG"),
        ({"secret_key": ""}, "DJANGO_SECRET_KEY"),
        ({"secret_key": "dev-insecure-change-me"}, "DJANGO_SECRET_KEY"),  # pragma: allowlist secret
        ({"secret_key": "short"}, "DJANGO_SECRET_KEY"),  # pragma: allowlist secret
        ({"field_encryption_key": ""}, "FIELD_ENCRYPTION_KEY"),
        ({"field_encryption_key": "not-fernet"}, "FIELD_ENCRYPTION_KEY"),
        ({"database_url": ""}, "DATABASE_URL"),
        ({"database_url": "postgres://db.invalid/runtime"}, "DATABASE_URL"),
        (
            {"database_url": REMOTE_DB_URL},
            "DATABASE_URL",
        ),
        (
            {"database_url": ENCODED_DB_URL},
            "DATABASE_URL",
        ),
        ({"hls_signing_key": ""}, "HLS_SIGNING_KEY"),
        ({"hls_signing_key": SECRET_KEY}, "HLS_SIGNING_KEY"),
        ({"hls_signing_key": "short"}, "HLS_SIGNING_KEY"),
        ({"hls_token_ttl_sec": 0}, "HLS_TOKEN_TTL_SEC"),
        ({"hls_token_ttl_sec": 86401}, "HLS_TOKEN_TTL_SEC"),
        ({"hls_token_ttl_sec": "3600"}, "HLS_TOKEN_TTL_SEC"),
        ({"allowed_hosts": []}, "ALLOWED_HOSTS"),
        ({"allowed_hosts": [""]}, "ALLOWED_HOSTS"),
        ({"allowed_hosts": ["*"]}, "ALLOWED_HOSTS"),
        ({"allowed_hosts": [".example.invalid"]}, "ALLOWED_HOSTS"),
        ({"allowed_hosts": "tv.example.invalid"}, "文字列のリスト"),
        ({"database_url": MALFORMED_DB_URL}, "形式を解析"),
    ],
)
def test_distribution_config_rejects_each_unsafe_condition(updates, expected):
    problems = check_distribution_config(**{**GOOD, **updates})
    assert any(expected in problem for problem in problems)


def test_distribution_error_does_not_echo_values():
    secret = "do-not-echo-django-secret"  # pragma: allowlist secret
    field_key = "do-not-echo-fernet-key"  # pragma: allowlist secret
    hls_key = "do-not-echo-hls-key"  # pragma: allowlist secret
    database_url = "postgres://%69CSTV:ic%73tv@hidden.invalid/runtime"  # pragma: allowlist secret
    config = {
        **GOOD,
        "secret_key": secret,
        "field_encryption_key": field_key,
        "database_url": database_url,
        "hls_signing_key": hls_key,
    }
    with pytest.raises(ImproperlyConfigured) as caught:
        enforce_distribution_config(**config)
    message = str(caught.value)
    for supplied in (secret, field_key, hls_key, database_url):
        assert supplied not in message


def test_default_database_comparison_is_safe_without_reference_password(monkeypatch):
    monkeypatch.setattr(checks, "INSECURE_DATABASE_PASSWORD", None)
    assert check_distribution_config(**GOOD) == []


def _settings_import_env(cache_root: Path, **updates: str) -> dict[str, str]:
    keep = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "TEMP",
            "TMP",
            "PATHEXT",
        }
    }
    keep.update(
        SYSTEMDRIVE="C:",
        PROGRAMDATA=str(cache_root / "programdata"),
        APPDATA=str(cache_root / "appdata"),
        LOCALAPPDATA=str(cache_root / "localappdata"),
        USERPROFILE=str(cache_root / "profile"),
        DJANGO_SETTINGS_MODULE="config.settings",
        ICSTV_DISTRIBUTION_MODE="true",
        DJANGO_DEBUG="false",
        DJANGO_SECRET_KEY=SECRET_KEY,
        DJANGO_ALLOWED_HOSTS="tv.example.invalid",
        DATABASE_URL=str(GOOD["database_url"]),
        ICSTV_FIELD_ENCRYPTION_KEY=FERNET_KEY,
        ICSTV_HLS_SIGNING_KEY=HLS_KEY,
        ICSTV_HLS_TOKEN_TTL_SEC="3600",
        AWS_EC2_METADATA_DISABLED="true",
    )
    keep.update(updates)
    return keep


def _import_settings(cache_root: Path, **updates: str) -> subprocess.CompletedProcess[str]:
    code = """
import socket
import os
import sys
def denied(*args, **kwargs):
    raise RuntimeError('network denied by distribution settings test')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
sys.path.insert(0, os.getcwd())
import config.settings
"""
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=_settings_import_env(cache_root, **updates),
        text=True,
        capture_output=True,
        check=False,
    )


def test_settings_import_wires_distribution_guard_without_network(tmp_path):
    result = _import_settings(tmp_path)
    assert result.returncode == 0, result.stderr


def test_settings_import_rejects_distribution_debug_before_startup(tmp_path):
    result = _import_settings(tmp_path, DJANGO_DEBUG="true")
    assert result.returncode != 0
    assert "ICSTV_DISTRIBUTION_MODE=true" in result.stderr
    assert SECRET_KEY not in result.stderr
    assert HLS_KEY not in result.stderr


def test_settings_import_leaves_distribution_guard_off_when_explicitly_false(tmp_path):
    result = _import_settings(
        tmp_path,
        ICSTV_DISTRIBUTION_MODE="false",
        DJANGO_DEBUG="true",
        DJANGO_SECRET_KEY="",
        ICSTV_FIELD_ENCRYPTION_KEY="",
        ICSTV_HLS_SIGNING_KEY="",
        DJANGO_ALLOWED_HOSTS="",
        DATABASE_URL="",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["1_0", "１", "١", "1.0", "0x1", "1e3", "+0", "-0"])
def test_settings_import_uses_shared_distribution_false_syntax(tmp_path, value):
    result = _import_settings(
        tmp_path,
        ICSTV_DISTRIBUTION_MODE=value,
        DJANGO_DEBUG="true",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["+2", "-1", "0" * 4500 + "1"])
def test_settings_import_uses_shared_distribution_true_syntax(tmp_path, value):
    result = _import_settings(
        tmp_path,
        ICSTV_DISTRIBUTION_MODE=value,
        DJANGO_DEBUG="true",
    )
    assert result.returncode != 0
    assert "ICSTV_DISTRIBUTION_MODE=true" in result.stderr


def test_settings_import_noninteger_ttl_names_setting_without_echoing_value(tmp_path):
    supplied = "do-not-echo-invalid-ttl"
    result = _import_settings(tmp_path, ICSTV_HLS_TOKEN_TTL_SEC=supplied)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "ICSTV_HLS_TOKEN_TTL_SEC" in result.stderr
    assert supplied not in result.stderr
