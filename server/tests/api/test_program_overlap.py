# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scheduling: program の時刻重なり禁止 (EXCLUDE USING gist) を実 DB で検証。

旧 scheduling/tests.py (TransactionTestCase) を pytest 形式へ移設。python_files=["test_*.py"]
の規約外で未収集だった休眠テストを tests/api/ 配下に統合し、確実に走らせる。
PostgreSQL の program_no_overlap_per_channel (tstzrange '[)' × channel EQUAL) が対象。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction

from core.models import Channel
from scheduling.models import Program, ProgramType

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _prog(channel, asset, start, end, title="P"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=end,
        asset=asset,
    )


def test_overlap_is_rejected_by_db(channel, asset_ready):
    _prog(channel, asset_ready, BASE, BASE + timedelta(hours=1), "A")
    with pytest.raises(IntegrityError), transaction.atomic():
        _prog(channel, asset_ready, BASE + timedelta(minutes=30), BASE + timedelta(minutes=90), "B")


def test_adjacent_is_allowed(channel, asset_ready):
    """tstzrange '[)' は隣接 (終端=次の開始) を重なりと見なさない。"""
    _prog(channel, asset_ready, BASE, BASE + timedelta(hours=1), "A")
    _prog(channel, asset_ready, BASE + timedelta(hours=1), BASE + timedelta(hours=2), "B")
    assert Program.objects.filter(channel=channel).count() == 2


def test_different_channel_overlap_is_allowed(channel, asset_ready):
    ch2 = Channel.objects.create(name="ch2", slug="ch2")
    _prog(channel, asset_ready, BASE, BASE + timedelta(hours=1), "A")
    _prog(ch2, asset_ready, BASE, BASE + timedelta(hours=1), "A on ch2")
    assert Program.objects.count() == 2
