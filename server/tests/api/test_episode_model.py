# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#5 納品ターゲティング: Episode (回) モデルの制約・派生 (docs/delivery.md)。"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from scheduling.models import Episode, EpisodeStatus, Series


def _series(channel):
    return Series.objects.create(channel=channel, title="毎週ドラマ")


def test_episode_no_partial_unique(channel):
    s = _series(channel)
    Episode.objects.create(series=s, episode_no=1)
    # 同 series 内で episode_no 重複は uq_episode_series_no 違反
    with pytest.raises(IntegrityError), transaction.atomic():
        Episode.objects.create(series=s, episode_no=1)


def test_episode_no_null_allows_multiple(channel):
    s = _series(channel)
    # 未採番 (episode_no=NULL) は条件付き unique の対象外 → 複数可
    Episode.objects.create(series=s, episode_no=None, air_date=None)
    Episode.objects.create(series=s, episode_no=None, air_date=None)
    assert Episode.objects.filter(series=s).count() == 2


def test_episode_no_unique_scoped_per_series(channel):
    s1 = _series(channel)
    s2 = Series.objects.create(channel=channel, title="別番組")
    # series が違えば同じ episode_no でも衝突しない
    Episode.objects.create(series=s1, episode_no=1)
    Episode.objects.create(series=s2, episode_no=1)
    assert Episode.objects.count() == 2


def test_episode_channel_derives_from_series(channel):
    s = _series(channel)
    ep = Episode.objects.create(series=s, episode_no=1)
    assert ep.channel == channel


def test_episode_defaults_to_planned(channel):
    s = _series(channel)
    ep = Episode.objects.create(series=s)
    assert ep.status == EpisodeStatus.PLANNED


def test_episode_asset_is_protected(channel, asset_ready):
    s = _series(channel)
    Episode.objects.create(series=s, episode_no=1, asset=asset_ready)
    # 回に紐づく Asset は削除保護 (on_delete=PROTECT)
    with pytest.raises(ProtectedError):
        asset_ready.delete()
