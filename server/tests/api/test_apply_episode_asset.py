# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#5 回別素材の asset-ready バックフィル (既に展開済みの Program へ反映 / docs/delivery.md)。"""

from __future__ import annotations

import datetime
from datetime import timedelta

from django.utils import timezone

from core.models import Notification
from medialib.models import Asset, AssetKind, NormalizeStatus
from scheduling.models import Episode, Program, ProgramType, Series, SeriesSlot
from scheduling.tasks import apply_episode_asset, expand_series_slots


def _series(channel):
    return Series.objects.create(channel=channel, title="毎週ドラマ")


def _asset(title, dur_ms, status=NormalizeStatus.READY):
    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title=title,
        duration_ms=dur_ms,
        r2_key=f"mezzanine/program/{title}.mp4",
        normalize_status=status,
    )


def _future_target():
    """今日+3日 (確実に未来) とその曜日。expand の最初の該当日になる。"""
    d = timezone.now().date() + timedelta(days=3)
    return d, d.weekday()


def test_apply_episode_asset_swaps_future_program(channel, asset_ready):
    s = _series(channel)
    target, dow = _future_target()
    SeriesSlot.objects.create(
        series=s,
        dow=dow,
        start_time="10:00",
        duration_ms=1_800_000,  # 既定枠 30分
        program_type=ProgramType.RECORDED,
        default_asset=asset_ready,
        effective_from=datetime.date(2020, 1, 1),
    )
    # 回別素材は当初 PENDING → expand は既定素材で展開し episode を絡めない
    ep_asset = _asset("ep-bf", 1_200_000, status=NormalizeStatus.PENDING)
    ep = Episode.objects.create(series=s, episode_no=4, air_date=target, asset=ep_asset)
    expand_series_slots(weeks=2)
    prog = Program.objects.filter(series=s).earliest("start_at")  # = target 回
    assert prog.asset_id == asset_ready.id and prog.episode_id is None

    # 正規化完了 (READY) → バックフィル
    ep_asset.normalize_status = NormalizeStatus.READY
    ep_asset.save(update_fields=["normalize_status"])
    res = apply_episode_asset(ep_asset.id)
    assert res["swapped"] == 1 and res["skipped"] == 0

    prog.refresh_from_db()
    assert prog.asset_id == ep_asset.id and prog.episode_id == ep.id
    # end_at は回別素材の尺 (20分) で再計算
    assert (prog.end_at - prog.start_at).total_seconds() * 1000 == 1_200_000
    # 別週 (target+7) の回は対象外 (air_date 単日マッチ)
    later = Program.objects.filter(series=s).latest("start_at")
    assert later.episode_id is None and later.asset_id == asset_ready.id


def test_apply_episode_asset_idempotent(channel, asset_ready):
    s = _series(channel)
    target, dow = _future_target()
    SeriesSlot.objects.create(
        series=s,
        dow=dow,
        start_time="10:00",
        duration_ms=1_800_000,
        program_type=ProgramType.RECORDED,
        default_asset=asset_ready,
        effective_from=datetime.date(2020, 1, 1),
    )
    ep_asset = _asset("ep-idem", 1_200_000)
    Episode.objects.create(series=s, episode_no=4, air_date=target, asset=ep_asset)
    expand_series_slots(weeks=2)
    apply_episode_asset(ep_asset.id)
    # 再実行は既に当該素材を持つため対象なし
    res = apply_episode_asset(ep_asset.id)
    assert res["swapped"] == 0 and res["skipped"] == 0


def test_apply_episode_asset_conflict_skips_and_notifies(channel):
    """長尺の回別素材が後続番組に食い込む場合は差替を見送り通知する (EXCLUDE 衝突)。"""
    s = _series(channel)
    target, _dow = _future_target()
    start = timezone.make_aware(datetime.datetime.combine(target, datetime.time(10, 0)))
    default_asset = _asset("def", 1_800_000)
    p1 = Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="回",
        start_at=start,
        end_at=start + timedelta(minutes=30),  # 10:00-10:30
        asset=default_asset,
    )
    # 隣接する後続番組 10:30-11:30 (同チャンネル)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="次番組",
        start_at=start + timedelta(minutes=30),
        end_at=start + timedelta(minutes=90),
        asset=_asset("nxt", 3_600_000),
    )
    # 回別素材 60分 → new_end=11:30 が後続 (10:30-) に食い込む
    ep_asset = _asset("ep-long", 3_600_000)
    Episode.objects.create(series=s, episode_no=7, air_date=target, asset=ep_asset)
    res = apply_episode_asset(ep_asset.id)
    assert res["swapped"] == 0 and res["skipped"] == 1
    p1.refresh_from_db()
    assert p1.asset_id == default_asset.id  # 差替されていない (保全)
    assert Notification.objects.filter(kind="episode_swap_conflict").exists()
