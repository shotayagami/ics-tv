# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube 連携が未設定・設定途中のときの beat タスクの挙動。

YouTube 連携は任意機能なので、未接続の導入者の環境でも beat が黙って何もしないこと、
設定途中 (livestream だけ作成済み・枠テンプレ未保存・OAuth 失効) の channel が
他 channel の枠生成を止めないことを確かめる。
"""

from __future__ import annotations

import logging

import pytest

from core.models import Channel
from youtube import tasks as yt_tasks
from youtube.models import YoutubeConfig, YoutubeCredential, YoutubeSlot

pytestmark = pytest.mark.django_db

ALL_TASKS = [
    "generate_slots_all",
    "rotate_slots_all",
    "nudge_next_slot_all",
    "generate_dedicated_broadcasts_all",
    "rotate_dedicated_all",
]


def _set_livestream(channel, value="LS-1"):
    channel.youtube_livestream_id = value
    channel.save(update_fields=["youtube_livestream_id"])


@pytest.mark.parametrize("name", ALL_TASKS)
def test_unconnected_channel_is_a_noop(channel, name):
    assert channel.youtube_livestream_id is None
    assert getattr(yt_tasks, name)() == {}


@pytest.mark.parametrize("livestream_id", ["", "LS-1"])
def test_generate_slots_without_saved_config_creates_nothing(channel, monkeypatch, livestream_id):
    _set_livestream(channel, livestream_id)

    def _must_not_call(*a, **kw):
        raise AssertionError("insert_broadcast must not run without a saved YoutubeConfig")

    monkeypatch.setattr(yt_tasks, "insert_broadcast", _must_not_call)
    assert yt_tasks.generate_slots_all() == {channel.id: {"created": 0, "skipped": 0}}
    assert not YoutubeSlot.objects.filter(channel=channel).exists()


def test_generate_slots_all_survives_missing_oauth_credential(channel, caplog):
    _set_livestream(channel)
    YoutubeConfig.objects.create(channel=channel, privacy="public")
    assert not YoutubeCredential.objects.filter(channel=channel).exists()
    with caplog.at_level(logging.ERROR, logger="youtube.tasks"):
        result = yt_tasks.generate_slots_all()
    assert result == {channel.id: {"errors": 1}}
    assert "youtube beat task failed" in caplog.text


def test_one_broken_channel_does_not_block_the_others(channel, monkeypatch):
    broken = channel
    healthy = Channel.objects.create(
        name="second", slug="ch2", enabled=True, agent_token="test-agent-token-second"
    )
    for ch in (broken, healthy):
        _set_livestream(ch)
        YoutubeConfig.objects.create(channel=ch, privacy="public")

    def _insert(ch, **kw):
        if ch.pk == broken.pk:
            raise YoutubeCredential.DoesNotExist("no credential for this channel")
        return f"BID-{kw['scheduled_start']:%H%M}"

    monkeypatch.setattr(yt_tasks, "insert_broadcast", _insert)
    result = yt_tasks.generate_slots_all()
    assert result[broken.id] == {"errors": 1}
    assert result[healthy.id]["created"] >= 1
    assert YoutubeSlot.objects.filter(channel=healthy).exists()
    assert not YoutubeSlot.objects.filter(channel=broken).exists()
