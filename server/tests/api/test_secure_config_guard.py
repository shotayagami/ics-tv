# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-5/M-6/I-4: 本番 fail-closed 設定ガードの単体テスト (config.checks)。"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.checks import (
    INSECURE_DATABASE_URL,
    INSECURE_SECRET_KEY,
    check_secure_config,
    enforce_secure_config,
)

_GOOD = {
    "secret_key": "a-real-long-random-secret-value",  # pragma: allowlist secret - test only
    "field_encryption_key": "a-real-fernet-key",  # pragma: allowlist secret - test only
    "database_url": "postgres://u:p@icstv-postgres:5432/icstv",  # pragma: allowlist secret - test only
}


def test_healthy_config_has_no_problems():
    assert check_secure_config(**_GOOD) == []


def test_default_secret_key_flagged():
    p = check_secure_config(**{**_GOOD, "secret_key": INSECURE_SECRET_KEY})
    assert len(p) == 1 and "SECRET_KEY" in p[0]


def test_empty_field_encryption_key_flagged():
    p = check_secure_config(**{**_GOOD, "field_encryption_key": ""})
    assert any("FIELD_ENCRYPTION_KEY" in x for x in p)


def test_default_database_url_flagged():
    p = check_secure_config(**{**_GOOD, "database_url": INSECURE_DATABASE_URL})
    assert any("DATABASE_URL" in x for x in p)


def test_all_insecure_yields_three_problems():
    p = check_secure_config(secret_key="", field_encryption_key="", database_url="")
    assert len(p) == 3


def test_enforce_raises_on_insecure():
    with pytest.raises(ImproperlyConfigured):
        enforce_secure_config(
            secret_key=INSECURE_SECRET_KEY, field_encryption_key="", database_url=""
        )


def test_enforce_passes_on_healthy():
    enforce_secure_config(**_GOOD)  # 例外が出なければ OK
