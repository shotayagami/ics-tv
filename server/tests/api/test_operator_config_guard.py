# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運営者表示 (特定商取引法11条相当) の fail-closed 設定ガードの単体テスト (config.checks)。"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.checks import check_operator_config, enforce_operator_config

_GOOD = {
    "legal_name": "架空商事株式会社",
    "representative_name": "架空 太郎",
    "address": "架空県架空市架空町1-2-3",
    "phone": "000-0000-0000",
    "hide_contact_details": False,
    "contact_email": "legal@example.invalid",
}


def test_healthy_config_has_no_problems():
    assert check_operator_config(**_GOOD) == []


def test_empty_legal_name_flagged():
    p = check_operator_config(**{**_GOOD, "legal_name": ""})
    assert any("LEGAL_NAME" in x for x in p)


def test_empty_representative_name_flagged():
    p = check_operator_config(**{**_GOOD, "representative_name": ""})
    assert any("REPRESENTATIVE_NAME" in x for x in p)


def test_empty_contact_email_flagged():
    p = check_operator_config(**{**_GOOD, "contact_email": ""})
    assert any("CONTACT_EMAIL" in x for x in p)


def test_disclosed_contact_requires_address_and_phone():
    p = check_operator_config(**{**_GOOD, "address": "", "phone": ""})
    assert any("ADDRESS" in x and "PHONE" in x for x in p)


def test_hide_contact_details_true_does_not_require_address_or_phone():
    """hide_contact_details=True の間は「請求により開示」に倒すため address/phone は必須にしない。"""
    p = check_operator_config(**{**_GOOD, "hide_contact_details": True, "address": "", "phone": ""})
    assert p == []


def test_all_unset_yields_three_problems():
    p = check_operator_config(
        legal_name="",
        representative_name="",
        address="",
        phone="",
        hide_contact_details=True,
        contact_email="",
    )
    assert len(p) == 3


def test_enforce_raises_on_unset():
    with pytest.raises(ImproperlyConfigured):
        enforce_operator_config(
            legal_name="",
            representative_name="",
            address="",
            phone="",
            hide_contact_details=True,
            contact_email="",
        )


def test_enforce_passes_on_healthy():
    enforce_operator_config(**_GOOD)  # 例外が出なければ OK
