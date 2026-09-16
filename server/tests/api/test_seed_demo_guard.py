# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-12: seed_demo は本番 (DEBUG=False) で既定拒否 (既知PW superuser バックドア防止)。"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings


@override_settings(DEBUG=False)
def test_seed_demo_blocked_when_not_debug(db):
    with pytest.raises(CommandError):
        call_command("seed_demo")
    # ガードは DB 書き込み前に発火 → デモ superuser は作られない
    assert not get_user_model().objects.filter(username="demo").exists()
