# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""フィラー再放送 EPG (Phase B): 編成に無いフィラー帯を「再放送」ブロックとして番組表/本日の番組へ。

project_filler_segments は表示専用投影で、送出経路 emit_filler のフレッシュ起点と同じ境界式
(_filler_cycle) を共有する。本テストは「投影 ≡ emit の境界」を固定し、rerun_eligible での選別と
guide/player API への結線を確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from medialib.models import Asset, AssetKind, FillerItem, FillerPlaylist, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent
from scheduling.resolver import emit_filler, project_filler_segments


def _filler_playlist(channel, specs):
    """specs = [(title, dur_ms, rerun_eligible), ...] を default_filler に設定。"""
    pl = FillerPlaylist.objects.create(name="rot")
    assets = []
    for i, (title, dur, rerun) in enumerate(specs, start=1):
        a = Asset.objects.create(
            kind=AssetKind.FILLER,
            title=title,
            duration_ms=dur,
            r2_key=f"mezzanine/filler/{i}.mp4",
            normalize_status=NormalizeStatus.READY,
            rerun_eligible=rerun,
        )
        FillerItem.objects.create(filler_playlist=pl, seq=i, asset=a)
        assets.append(a)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])
    return assets


def test_projection_matches_emit_boundaries(channel):
    """未来ギャップ (鎖状継続なし) では投影の (start, asset) が emit_filler の (scheduled_at, asset) と一致。"""
    a0, a1 = _filler_playlist(channel, [("f0", 60_000, True), ("f1", 120_000, True)])
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = base + timedelta(minutes=6)

    evs = emit_filler(channel, base, end, not_before=base)
    segs = project_filler_segments(channel, base, end)

    assert [(e.scheduled_at, e.asset_id) for e in evs] == [(s.start_at, s.id) for s in segs]
    # サイクル: a0(0-1分), a1(1-3分), a0(3-4分), a1(4-6分)
    assert [s.id for s in segs] == [a0.id, a1.id, a0.id, a1.id]


def test_projection_resumes_from_interruption_offset(channel):
    """投影も送出と同じく実番組明けは、中断されたクリップの中断位置から再開する。

    a0(60s) を 30s で実番組が割り込む → 番組明けの再放送投影は a0 を残り 30s 分だけ続けてから
    通常ローテに戻る (その分だけ a0 の見かけ上の占有時間が延びる)。
    """
    from core.models import LiveSource
    from scheduling.models import Program, ProgramType

    a0, a1 = _filler_playlist(channel, [("f0", 60_000, True), ("f1", 120_000, True)])
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    prog_end = base + timedelta(seconds=30, minutes=10)
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="生中継",
        start_at=base + timedelta(seconds=30),
        end_at=prog_end,
        live_source=ls,
    )
    segs = project_filler_segments(channel, base, base + timedelta(hours=1))

    # 先頭ギャップ: a0 を base から (番組まで 30s)。
    assert segs[0].start_at == base
    assert segs[0].id == a0.id

    after = [s for s in segs if s.start_at >= prog_end]
    # 番組明け = 中断された a0 自身を、残り尺 (30s) だけ再開してから通常ローテに戻る。
    assert after[0].start_at == prog_end
    assert after[0].id == a0.id
    assert after[0].end_at == prog_end + timedelta(seconds=30)
    assert [s.id for s in after[1:4]] == [a1.id, a0.id, a1.id]


def test_projection_follows_emitted_events(channel):
    """発行済み PLAY_FILLER が窓に掛かる範囲は、純計算でなくイベントの asset/区間を真として出す。

    実放送は鎖状継続でローテ位置 (pl_idx) が純計算の起点と必ずしも一致しない。番組表が実放送と
    ズレないよう、投影は発行済みイベントをそのまま採用する (本不具合の根治)。
    """
    _a0, _a1, a2 = _filler_playlist(
        channel, [("f0", 600_000, True), ("f1", 600_000, True), ("f2", 600_000, True)]
    )
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # 純計算なら idx0(a0) から始まるはずの窓に、実発行は idx2(a2) を流している。
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=base,
        action=PlayoutAction.PLAY_FILLER,
        asset=a2,
        params={"pl_idx": 2, "until": (base + timedelta(minutes=10)).isoformat()},
    )
    segs = project_filler_segments(channel, base, base + timedelta(minutes=10))

    assert [s.id for s in segs] == [a2.id], "発行済みイベントでなく純計算の並びを出している"
    assert segs[0].start_at == base


def test_projection_extrapolates_tail_after_last_event(channel):
    """発行ホライズン先 (イベントが無いテール) は最後の pl_idx の「次クリップ」から鎖状継続。"""
    a0, a1, a2 = _filler_playlist(
        channel, [("f0", 600_000, True), ("f1", 600_000, True), ("f2", 600_000, True)]
    )
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=base,
        action=PlayoutAction.PLAY_FILLER,
        asset=a0,
        params={"pl_idx": 0, "until": (base + timedelta(minutes=10)).isoformat()},
    )
    segs = project_filler_segments(channel, base, base + timedelta(minutes=40))

    # 実イベント a0(12:00-12:10) → テールは次クリップ a1, a2, a0 と継ぐ。
    assert [s.id for s in segs] == [a0.id, a1.id, a2.id, a0.id]
    assert [s.start_at for s in segs] == [
        base,
        base + timedelta(minutes=10),
        base + timedelta(minutes=20),
        base + timedelta(minutes=30),
    ]


def test_projection_filters_non_rerun_eligible(channel):
    """rerun_eligible でないクリップ (局ID/プロモ等) は投影セグメントに出さない (境界計算には含む)。"""
    a0, _a1 = _filler_playlist(channel, [("番組再放送", 60_000, True), ("局ID", 120_000, False)])
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    segs = project_filler_segments(channel, base, base + timedelta(minutes=6))

    # a0 のみ (0-1分, 3-4分) が出る。a1 (局ID) は除外。境界は a1 尺ぶんずれて維持される。
    assert all(s.id == a0.id for s in segs)
    assert [s.start_at for s in segs] == [base, base + timedelta(seconds=180)]


def test_projection_excludes_off_air_pure_calc(channel):
    """放送休止中 (broadcast_windows 外) は実際には PLAY_SLATE が流れるため、純計算投影でも
    再放送ブロックを出さない (#YouTube 放送時間帯機能に伴う番組表側の追従修正)。"""
    a0, a1 = _filler_playlist(channel, [("f0", 60_000, True), ("f1", 120_000, True)])
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)  # = 09:00 JST
    channel.broadcast_windows = [{"start": "09:00", "end": "09:02"}]  # 放送は最初の2分のみ
    channel.save(update_fields=["broadcast_windows"])

    segs = project_filler_segments(channel, base, base + timedelta(minutes=6))

    # サイクルは a0(0-1分)/a1(1-3分)/a0(3-4分)/a1(4-6分) だが、休止開始 (09:02) 以降は出さない。
    assert [s.id for s in segs] == [a0.id, a1.id]
    assert [s.start_at for s in segs] == [base, base + timedelta(minutes=1)]


def test_projection_excludes_off_air_tail_extrapolation(channel):
    """発行ホライズン先のテール外挿でも、休止時間帯に入るクリップは出さない。"""
    a0, a1, _a2 = _filler_playlist(
        channel, [("f0", 600_000, True), ("f1", 600_000, True), ("f2", 600_000, True)]
    )
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # = 21:00 JST
    channel.broadcast_windows = [{"start": "21:00", "end": "21:20"}]  # 放送は 20 分のみ
    channel.save(update_fields=["broadcast_windows"])
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=base,
        action=PlayoutAction.PLAY_FILLER,
        asset=a0,
        params={"pl_idx": 0, "until": (base + timedelta(minutes=10)).isoformat()},
    )

    segs = project_filler_segments(channel, base, base + timedelta(minutes=40))

    # 実イベント a0(12:00-12:10, 在-window) → テール a1(12:10-12:20, 在-window)。
    # a2(12:20-, 休止開始) 以降は出さない。
    assert [s.id for s in segs] == [a0.id, a1.id]


def test_projection_excludes_off_air_emitted_event(channel):
    """発行済み PLAY_FILLER でも休止時間帯 (broadcast_windows 外) の scheduled_at のものは
    再放送帯として投影しない。

    旧実装は発行済みイベント経路だけ is_on_air ガードが抜けており、休止設定前に発行された古い
    PLAY_FILLER が窓外に残っていると番組表の休止帯へ再放送が漏れて発覚しうる。純計算/テール
    外挿経路 (test_projection_excludes_off_air_*) と同じく on-air のイベントのみ出す。
    """
    a0, a1 = _filler_playlist(channel, [("f0", 600_000, True), ("f1", 600_000, True)])
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # = 21:00 JST
    channel.broadcast_windows = [{"start": "21:00", "end": "21:10"}]  # 放送は最初の10分のみ
    channel.save(update_fields=["broadcast_windows"])
    # 在-window の発行イベント (21:00-21:10) と 休止帯の発行イベント (21:10-21:20)。
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=base,
        action=PlayoutAction.PLAY_FILLER,
        asset=a0,
        params={"pl_idx": 0, "until": (base + timedelta(minutes=10)).isoformat()},
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=base + timedelta(minutes=10),
        action=PlayoutAction.PLAY_FILLER,
        asset=a1,
        params={"pl_idx": 1, "until": (base + timedelta(minutes=20)).isoformat()},
    )

    segs = project_filler_segments(channel, base, base + timedelta(minutes=20))

    # a0 (21:00, 在-window) のみ。a1 (21:10, 休止帯) は漏らさない。
    assert [s.id for s in segs] == [a0.id]
    assert segs[0].start_at == base


@pytest.mark.django_db(transaction=True)
def test_resolve_does_not_restart_leading_filler_across_reruns_with_broadcast_windows(channel):
    """broadcast_windows 有効時、resolve を周期的に再実行しても送出中フィラーを途中で奪わない。

    回帰: 放送休止明けの先頭ギャップは _on_off_segs が on_intervals を resolve 呼び出しの
    t0=now でクリップするため、seg_s <= anchor という値比較では 2 回目以降の resolve で
    「後続 on 区間」と誤判定され、anchor (安定した番組明け位相) でなく seg_s (= 前進する now)
    を使ってしまい、送出中クリップが 5 分おき (resolve 周期) に pl_idx=0 から再発行され続けて
    いた (実運用で番組表に同一クリップの短い重複ブロックが大量に出る形で発覚)。
    """
    from core.models import LiveSource
    from playout.models import PlayoutStatus
    from scheduling.models import Program, ProgramType
    from scheduling.resolver import resolve

    a0, _a1 = _filler_playlist(channel, [("f0", 60_000, True), ("f1", 120_000, True)])
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)  # = 09:00 JST
    channel.broadcast_windows = [{"start": "09:00", "end": "11:00"}]
    channel.save(update_fields=["broadcast_windows"])
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="前番組",
        start_at=base - timedelta(minutes=10),
        end_at=base,  # 番組明け = 放送開始と同時刻 (prev_end を安定した anchor にする)
        live_source=ls,
    )

    resolve(channel, base, base + timedelta(seconds=20))
    # 10 秒後の周期再実行 (now が前進しても anchor は変わらない想定)。
    resolve(channel, base + timedelta(seconds=10), base + timedelta(seconds=40))

    live = list(
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_FILLER)
        .exclude(status=PlayoutStatus.CANCELLED)
        .order_by("scheduled_at")
    )
    # 2 回目の resolve が同一クリップを別 scheduled_at で再発行していないこと (=重複が無い)。
    assert [e.scheduled_at for e in live] == [base]
    assert live[0].asset_id == a0.id


def test_projection_empty_without_default_filler(channel):
    """default_filler 未設定なら投影は空 (従来どおり EPG は空白)。"""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert project_filler_segments(channel, base, base + timedelta(hours=1)) == []


def test_guide_api_includes_rerun_blocks(channel):
    """/api/v1/guide: 編成が無い当日帯が再放送ブロック (is_rerun) で埋まる。"""
    _filler_playlist(channel, [("懐かし劇場", 3_600_000, True)])  # 1h クリップ
    d = Client().get("/api/v1/guide").json()
    blocks = d["cols"][0]["blocks"]
    rerun_blocks = [b for b in blocks if b["is_rerun"]]
    assert rerun_blocks, "再放送ブロックが番組表に出ていない"
    assert rerun_blocks[0]["title"] == "懐かし劇場"


def test_player_day_list_includes_rerun(channel):
    """/api/v1/channels/{slug}: 本日の番組リストに再放送行 (is_rerun) が混ざる。"""
    _filler_playlist(channel, [("懐かし劇場", 3_600_000, True)])
    d = Client().get(f"/api/v1/channels/{channel.slug}").json()
    reruns = [r for r in d["day_list"] if r["is_rerun"]]
    assert reruns, "本日の番組に再放送行が出ていない"
    assert reruns[0]["title"] == "懐かし劇場"


@override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
def test_week_epg_includes_rerun(channel):
    """週間番組表 (SSR /guide/week/): 編成が無い帯に再放送ブロックが出る (公開 urlconf)。"""
    _filler_playlist(channel, [("懐かし劇場", 3_600_000, True)])
    html = Client().get(f"/guide/week/?ch={channel.slug}").content.decode("utf-8")
    assert "再放送" in html
    assert "懐かし劇場" in html


def test_guide_blocks_sorted_by_time(channel):
    """編成 Program と再放送ブロックが時刻順 (top 昇順) に整列している。"""
    from scheduling.models import Program

    now = timezone.now()
    ds = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    prog_asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="昼の番組本編",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/noon.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    Program.objects.create(
        channel=channel,
        title="昼の番組",
        type="recorded",
        asset=prog_asset,
        start_at=ds + timedelta(hours=12),
        end_at=ds + timedelta(hours=13),
        public_visible=True,
    )
    _filler_playlist(channel, [("懐かし劇場", 3_600_000, True)])

    blocks = Client().get("/api/v1/guide").json()["cols"][0]["blocks"]
    tops = [b["top"] for b in blocks]
    assert tops == sorted(tops)
    assert any(b["is_rerun"] for b in blocks) and any(not b["is_rerun"] for b in blocks)
