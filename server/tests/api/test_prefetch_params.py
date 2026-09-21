# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""prefetch (overview 3.2): mezzanine r2_key を params に載せ、gRPC 配信時に presigned media_url 付与。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.utils import timezone

from core.models import LiveSource
from medialib.models import (
    Asset,
    AssetKind,
    CmCreative,
    CmGrid,
    FillerItem,
    FillerPlaylist,
    NormalizeStatus,
)
from playout.grpc_service import _build_prefetch_manifest, _event_to_proto
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import AdBreak, Program, ProgramType
from scheduling.resolver import _ikey, emit_filler, resolve


def _cm(name="c"):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=name,
        duration_ms=15000,
        r2_key=f"mezzanine/cm/{name}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def test_resolve_play_asset_carries_r2_key(channel, asset_ready):
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now + timedelta(minutes=1),
        end_at=now + timedelta(minutes=1, hours=1),
        asset=asset_ready,
    )
    resolve(channel, now, now + timedelta(hours=2))
    ev = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_ASSET).first()
    assert ev is not None
    assert ev.params.get("r2_key") == "mezzanine/program/ep1.mp4"


def test_resolve_play_cm_carries_r2_key(channel, asset_ready):
    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now + timedelta(minutes=1),
        end_at=now + timedelta(minutes=1, seconds=15, hours=1),
        asset=asset_ready,
    )
    AdBreak.objects.create(program=prog, offset_ms=1_800_000, grid=CmGrid.G15, duration_ms=15000)
    cm = _cm("spotx")
    resolve(channel, now, now + timedelta(hours=2))
    ev = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_CM).first()
    assert ev is not None
    assert ev.params.get("r2_key") == cm.asset.r2_key


def test_event_to_proto_adds_presigned_media_url(channel, monkeypatch):
    monkeypatch.setattr(
        "core.r2.presign_get", lambda key, expires=600: f"https://signed/{key}?e={expires}"
    )
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_ASSET,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": "asset/1", "r2_key": "mezzanine/program/1.mp4"},
    )
    msg = _event_to_proto(ev)
    assert msg.params["media_url"] == "https://signed/mezzanine/program/1.mp4?e=518400"


def test_event_to_proto_no_media_url_without_r2_key(channel):
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": "filler/default"},
    )
    msg = _event_to_proto(ev)
    assert "media_url" not in msg.params


def test_emit_filler_carries_r2_key_when_default_filler_set(channel):
    """既定フィラー (FillerPlaylist) が正規化済なら filler も本編と同じ R2 prefetch 経路に乗る。"""
    now = timezone.now()
    a = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="evergreen",
        duration_ms=60000,
        r2_key="mezzanine/filler/9.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    pl = FillerPlaylist.objects.create(name="ch default")
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    evs = emit_filler(channel, now, now + timedelta(minutes=10), not_before=now)
    # 10分 ÷ 60秒クリップ = 10 本を 60 秒境界で全尺シーケンス (途中切れしない)
    assert len(evs) == 10
    assert all(pe.params["clip"] == f"filler/{a.id}" for pe in evs)
    assert all(pe.params["r2_key"] == "mezzanine/filler/9.mp4" for pe in evs)
    assert all(pe.params["loop"] is True for pe in evs)
    assert all(pe.asset_id == a.id for pe in evs)
    assert evs[0].scheduled_at == now
    assert evs[1].scheduled_at == now + timedelta(seconds=60)


def test_emit_filler_fallback_without_default_filler(channel):
    """既定フィラー未設定なら従来どおり filler/default・r2_key 無し (ノードローカル/agent slate)。"""
    now = timezone.now()
    evs = emit_filler(channel, now, now + timedelta(minutes=10), not_before=now)
    assert len(evs) == 1
    pe = evs[0]
    assert pe.params["clip"] == "filler/default"
    assert "r2_key" not in pe.params


def test_build_prefetch_manifest_lists_fillers_and_slate(channel, monkeypatch):
    """_build_prefetch_manifest が default_filler 全件 + slate を presigned 付きで返す。"""
    monkeypatch.setattr("core.r2.presign_get", lambda key, expires=600: f"https://signed/{key}")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f0",
        duration_ms=60000,
        r2_key="mezzanine/filler/2.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f1",
        duration_ms=60000,
        r2_key="mezzanine/filler/3.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    pl = FillerPlaylist.objects.create(name="standing")
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    slate = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="slate",
        duration_ms=10000,
        r2_key="mezzanine/slate/9.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    channel.default_filler = pl
    channel.slate_asset = slate
    channel.save(update_fields=["default_filler", "slate_asset"])

    manifest = _build_prefetch_manifest(channel)
    clips = {m["clip"] for m in manifest}
    assert clips == {f"filler/{a0.id}", f"filler/{a1.id}", f"slate/{slate.id}"}
    assert all(m["url"].startswith("https://signed/") for m in manifest)
    slate_items = [m for m in manifest if m.get("slate")]
    assert len(slate_items) == 1
    assert slate_items[0]["clip"] == f"slate/{slate.id}"


def test_event_to_proto_injects_manifest_for_filler_only(channel):
    """渡した manifest は filler イベントの params にだけ乗る (本編/CM には乗らない)。"""
    import json

    mani = [{"clip": "filler/2", "url": "https://s/2.mp4"}]
    fev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": "filler/2"},
    )
    assert (
        json.loads(_event_to_proto(fev, prefetch_manifest=mani).params["prefetch_manifest"]) == mani
    )
    # 本編には manifest を渡しても乗らない (時間変動物を常駐させない)
    aev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_ASSET,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": "asset/1"},
    )
    assert "prefetch_manifest" not in _event_to_proto(aev, prefetch_manifest=mani).params


def test_fetch_events_after_builds_manifest_for_filler(channel, monkeypatch):
    """_fetch_events_after が filler イベント時に manifest を sync 文脈で構築して返す。

    async ストリームから _build_prefetch_manifest を直接呼ぶと SynchronousOnlyOperation で
    落ちる回帰を防ぐ (manifest 構築は @sync_to_async の fetch 内に置く)。
    """
    from asgiref.sync import async_to_sync

    from playout.grpc_service import _fetch_events_after

    monkeypatch.setattr("core.r2.presign_get", lambda key, expires=600: f"https://signed/{key}")
    a = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f",
        duration_ms=60000,
        r2_key="mezzanine/filler/2.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    pl = FillerPlaylist.objects.create(name="pl")
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": f"filler/{a.id}"},
    )
    _events, manifest = async_to_sync(_fetch_events_after)(channel.slug, 0, 100)
    assert manifest is not None
    assert any(m["clip"] == f"filler/{a.id}" for m in manifest)


def test_fetch_events_after_no_manifest_without_filler(channel):
    """filler イベントが無ければ manifest は None (時間変動物だけなら常駐構築しない)。"""
    from asgiref.sync import async_to_sync

    from playout.grpc_service import _fetch_events_after

    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_ASSET,
        status=PlayoutStatus.SCHEDULED,
        params={"clip": "asset/1"},
    )
    _events, manifest = async_to_sync(_fetch_events_after)(channel.slug, 0, 100)
    assert manifest is None


def test_emit_filler_sequences_through_playlist(channel):
    """default_filler が複数本なら 1 ギャップ内で実尺どおりに巡回する (各クリップ全尺→次クリップ)。"""
    pl = FillerPlaylist.objects.create(name="rot")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f0",
        duration_ms=60000,
        r2_key="mezzanine/filler/2.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f1",
        duration_ms=120000,  # 2分クリップ (尺が違っても実尺で並ぶことを確認)
        r2_key="mezzanine/filler/3.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    evs = emit_filler(channel, base, base + timedelta(minutes=6), not_before=base)
    # playlist 周期 = 60+120 = 180s。境界: 0(a0),60(a1),180(a0),240(a1),360-end... → 6分で
    # a0(0-1分),a1(1-3分),a0(3-4分),a1(4-6分) の 4 本 (各クリップ実尺どおり)。
    assert [pe.asset_id for pe in evs] == [a0.id, a1.id, a0.id, a1.id]
    assert [pe.scheduled_at for pe in evs] == [
        base,
        base + timedelta(seconds=60),
        base + timedelta(seconds=180),
        base + timedelta(seconds=240),
    ]


def test_resolve_filler_resumes_from_interruption_offset(channel):
    """実番組明けのフィラーは playlist 先頭でも「次のクリップ」でもなく、中断されたクリップの
    中断位置から (残り尺だけ SEEK して) 再開する。

    a0(60s) 再生 30s で実番組が割り込み (a0 を中断) → 番組明けは a0 を in_ms=30000 (残り 30s) で
    再開し、以降は通常ローテを継続する。
    """
    pl = FillerPlaylist.objects.create(name="rot")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f0",
        duration_ms=60000,
        r2_key="mezzanine/filler/0.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f1",
        duration_ms=120000,
        r2_key="mezzanine/filler/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    now = timezone.now()
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,  # 生番組 (asset 尺不要) で a0 を 30s 時点で割り込む
        title="生中継",
        start_at=now + timedelta(seconds=30),
        end_at=now + timedelta(seconds=30, minutes=10),
        live_source=ls,
    )
    resolve(channel, now, now + timedelta(hours=1))

    fillers = list(
        PlayoutEvent.objects.filter(
            channel=channel,
            action=PlayoutAction.PLAY_FILLER,
            status=PlayoutStatus.SCHEDULED,
        ).order_by("scheduled_at")
    )
    # 先頭ギャップ: a0 を now から (番組まで 30s で中断)。
    assert fillers[0].scheduled_at == now
    assert fillers[0].asset_id == a0.id

    after = [f for f in fillers if f.scheduled_at >= prog.end_at]
    # 番組明け = 中断された a0 自身を、中断位置 (30000ms) から残り尺 (30000ms) だけ再開する。
    assert after[0].scheduled_at == prog.end_at
    assert after[0].asset_id == a0.id
    assert after[0].params["pl_idx"] == 0
    assert after[0].params["in_ms"] == 30000
    assert after[0].params["out_ms"] == 60000
    assert after[0].params["until"] == (prog.end_at + timedelta(seconds=30)).isoformat()
    # 以降は通常ローテを継続: a1(フル120s) → a0(フル60s) → a1。
    assert [f.asset_id for f in after[1:4]] == [a1.id, a0.id, a1.id]
    assert "in_ms" not in after[1].params  # 通常クリップは in_ms/out_ms を持たない


def test_resolve_filler_after_program_is_idempotent(channel):
    """実番組明けの中断→再開フィラーは再 resolve しても scheduled_at / asset / in_ms が不変 (冪等)。"""
    pl = FillerPlaylist.objects.create(name="rot")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f0",
        duration_ms=60000,
        r2_key="mezzanine/filler/0.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f1",
        duration_ms=90000,
        r2_key="mezzanine/filler/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    now = timezone.now()
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k2")
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="生中継",
        start_at=now + timedelta(seconds=45),
        end_at=now + timedelta(seconds=45, minutes=5),
        live_source=ls,
    )

    def _after_program():
        resolve(channel, now, now + timedelta(hours=1))
        return [
            (f.scheduled_at, f.asset_id, f.params.get("in_ms"))
            for f in PlayoutEvent.objects.filter(
                channel=channel,
                action=PlayoutAction.PLAY_FILLER,
                scheduled_at__gte=prog.end_at,
            ).order_by("scheduled_at")
        ]

    first = _after_program()
    second = _after_program()
    assert first == second
    assert first, "番組明けにフィラーが発行されていない"
    # a0(60s) は 45s 時点で中断 → 番組明けは in_ms=45000 (残り15s) で再開する。
    assert first[0] == (prog.end_at, a0.id, 45000)


def test_emit_filler_chains_from_onair_unaffected_by_playlist_growth(channel):
    """送出中フィラーがあれば、playlist 追加で total が変わっても「次境界」は動かない。

    = 再生中クリップを途中で奪わない (長尺追加→正規化完了で total 激変→途中切りした不具合の回帰防止)。
    """
    pl = FillerPlaylist.objects.create(name="rot")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f0",
        duration_ms=60000,
        r2_key="mezzanine/filler/0.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="f1",
        duration_ms=120000,
        r2_key="mezzanine/filler/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    now = timezone.now()
    anchor = now - timedelta(seconds=30)
    # a0 (60s) が anchor から送出中 (30s 経過)。確定済み event として DB にある。
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=anchor,
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.DONE,
        actual_at=anchor,
        asset=a0,
        params={"clip": f"filler/{a0.id}", "pl_idx": 0},
    )

    # ここで長尺を追加 (total が 180s → 3780s に激変)。旧実装はグリッドがずれて a0 を途中で奪っていた。
    a2 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="long",
        duration_ms=3600000,  # 60分
        r2_key="mezzanine/filler/2.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=3, asset=a2)

    evs = emit_filler(channel, anchor, now + timedelta(hours=1), not_before=now)
    # 次境界は on-air a0 の実終端 (= anchor + 60s) で不変。a0 は途中で奪われない。
    assert evs[0].scheduled_at == anchor + timedelta(seconds=60)
    assert evs[0].asset_id == a1.id
    assert evs[0].params["pl_idx"] == 1
    # 鎖状にローテ継続: a1(120s) → a2(60分)。
    assert evs[1].scheduled_at == anchor + timedelta(seconds=180)
    assert evs[1].asset_id == a2.id


def test_emit_filler_stable_anchor_is_not_preempted(channel):
    """anchor 固定なら、now (not_before) が進んでも既存境界の scheduled_at は不変。

    = 再生中の長尺クリップを途中で奪わない (旧 5 分ローテーション不具合の回帰防止)。
    """
    pl = FillerPlaylist.objects.create(name="long")
    a = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="ten-min",
        duration_ms=600000,  # 10分クリップ
        r2_key="mezzanine/filler/x.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])

    anchor = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = anchor + timedelta(hours=1)
    # gap 開始時の resolve と、その 5 分後の再 resolve (now=not_before が進む)
    sched0 = [e.scheduled_at for e in emit_filler(channel, anchor, end, not_before=anchor)]
    sched5 = [
        e.scheduled_at
        for e in emit_filler(channel, anchor, end, not_before=anchor + timedelta(minutes=5))
    ]
    # 境界は 0,10,20,... 分で安定。5 分後の再 resolve でも時刻は不変で、過ぎた境界が落ちるだけ。
    assert sched5 == [s for s in sched0 if s >= anchor + timedelta(minutes=5)]
    # 0 分開始の 10 分クリップは 5 分時点では奪われず、次境界(10分)まで全尺再生される。
    assert anchor + timedelta(minutes=10) in sched5
    assert anchor not in sched5  # 再生中(0分境界)は再発行されない (継続中の event が担う)


# ---- 放送休止 (broadcast_windows) をまたぐフィラー位相の回帰 ----
#
# 本番障害 (2026-07, ch1) の再現。休止中も本線 (layer 10) に PLAY_FILLER が撒かれてフィラーが
# 実時間で進む一方、位相計算 (_on_air_gap_ms) は放送中時間のみを積算していたため、休止をまたいだ
# クリップを実番組が中断すると再開位置が「休止中に再生された分」だけ手前に戻っていた
# (実測: 実位置 1:21:31.8 に対し再開 in_ms が 57:00.0 = 24分31.8秒の巻き戻り)。
# 併せて、休止中に開始する (=送出スキップされる) 番組の end_at が位相アンカーを壊し、次の番組明けが
# 中断位置ではなく無関係なクリップへのハードカットになる欠陥も固定する。

_PAUSE_WINDOWS = [{"start": "09:00", "end": "11:00"}]  # JST 09-11 = UTC 00:00-02:00


def _pause_channel(channel):
    """UTC 00:00-02:00 のみ放送、a0(60分)/a1(30分) のフィラー playlist を持つ channel。"""
    pl = FillerPlaylist.objects.create(name="pause-rot")
    a0 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="p0",
        duration_ms=3_600_000,
        r2_key="mezzanine/filler/p0.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    a1 = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="p1",
        duration_ms=1_800_000,
        r2_key="mezzanine/filler/p1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=a0)
    FillerItem.objects.create(filler_playlist=pl, seq=2, asset=a1)
    channel.default_filler = pl
    channel.broadcast_windows = _PAUSE_WINDOWS
    channel.save(update_fields=["default_filler", "broadcast_windows"])
    return a0, a1


def _onair_filler(channel, asset, at, pl_idx=0, **params):
    """「送出中」の PLAY_FILLER を 1 件だけ作る (resolve の位相復元の起点)。"""
    return PlayoutEvent.objects.create(
        channel=channel,
        idempotency_key=_ikey(channel.id, at, PlayoutAction.PLAY_FILLER, "0"),
        scheduled_at=at,
        action=PlayoutAction.PLAY_FILLER,
        status=PlayoutStatus.DONE,
        asset=asset,
        params={"pl_idx": pl_idx, "clip": f"filler/{asset.id}", **params},
    )


def test_resolve_emits_no_filler_during_off_air(channel):
    """放送休止時間帯には PLAY_FILLER を 1 件も発行しない (本線は休止直前のクリップを LOOP)。

    回帰: 休止中の t0 でも emit_filler が not_before(=now) 以降を埋めていたため、スレート
    (layer 90) の下で layer 10 のフィラーが実時間で進み、位相計算と乖離していた。
    """
    a0, _a1 = _pause_channel(channel)
    _onair_filler(channel, a0, datetime(2026, 1, 1, 1, 30, tzinfo=UTC))

    # t0 は休止中。窓オープン (翌 00:00) は発行ホライズン (+3h) の内側。
    resolve(
        channel, datetime(2026, 1, 1, 23, 0, tzinfo=UTC), datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    )

    off_air = [
        e
        for e in PlayoutEvent.objects.filter(
            channel=channel, action=PlayoutAction.PLAY_FILLER
        ).exclude(status=PlayoutStatus.CANCELLED)
        if not channel.is_on_air(e.scheduled_at)
    ]
    assert off_air == []


def test_resolve_filler_resumes_at_window_open_from_frozen_offset(channel):
    """休止明け (窓オープン) は、休止に入った瞬間の位置から in_ms 付きで再開する。

    a0(60分) が 01:30 に開始 → 02:00 の窓クローズ時点で 30 分再生済 → 22 時間の休止をはさみ、
    翌 00:00 の窓オープンで a0 を in_ms=1,800,000 (残り 30 分) から再開する。
    """
    a0, _a1 = _pause_channel(channel)
    _onair_filler(channel, a0, datetime(2026, 1, 1, 1, 30, tzinfo=UTC))

    # t0 は休止中。窓オープン (翌 00:00) は発行ホライズン (+3h) の内側。
    resolve(
        channel, datetime(2026, 1, 1, 23, 0, tzinfo=UTC), datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    )

    win_open = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    ev = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_FILLER, scheduled_at=win_open
    )
    assert ev.asset_id == a0.id
    assert ev.params["pl_idx"] == 0
    assert ev.params["in_ms"] == 1_800_000  # 01:30->02:00 の 30 分だけ進んでいる
    assert ev.params["out_ms"] == 3_600_000


def test_resolve_filler_resume_offset_after_pause_crossing_program(channel):
    """休止をまたいだクリップを実番組が中断したとき、再開位置は「実際に放送された位置」になる。

    本番障害の直接再現。窓オープン(00:00)で a0 が in_ms=1,800,000 から再開 → 00:10 に 3 分番組が
    割り込み → 番組明け(00:13)の再開は 1,800,000 + 600,000 = 2,400,000。休止時間を実時間として
    数えてしまうと手前に、休止中もフィラーを進めてしまうと先に、それぞれズレる。
    """
    a0, _a1 = _pause_channel(channel)
    _onair_filler(channel, a0, datetime(2026, 1, 1, 1, 30, tzinfo=UTC))
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    prog_end = datetime(2026, 1, 2, 0, 13, tzinfo=UTC)
    Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="休止明けの生番組",
        start_at=datetime(2026, 1, 2, 0, 10, tzinfo=UTC),
        end_at=prog_end,
        live_source=ls,
    )

    # t0 は休止中。窓オープン (翌 00:00) は発行ホライズン (+3h) の内側。
    resolve(
        channel, datetime(2026, 1, 1, 23, 0, tzinfo=UTC), datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    )

    ev = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_FILLER, scheduled_at=prog_end
    )
    assert ev.asset_id == a0.id
    assert ev.params["in_ms"] == 2_400_000
    assert ev.params["out_ms"] == 3_600_000


def test_resolve_filler_phase_ignores_off_air_skipped_program(channel):
    """休止中に開始する (= 送出スキップされる) 番組の end_at で位相アンカーを前進させない。

    回帰: _last_program_end_before が窓外開始の番組も拾うため、その end_at が窓オープンより後だと
    prev_end が「実際には放送されていない番組の終了時刻」になり、送出中フィラー (窓オープンに発行
    済み) が _onair_filler_anchor の探索窓 [prev_end, now] から外れて鎖状継続が切れていた。その結果
    位相が playlist 先頭にリセットされ、次の番組明けが中断位置ではなく無関係なクリップへのカットに
    なっていた (本番 2026-07-26 の 17:00 JST に毎日再現していた事象)。
    """
    a0, a1 = _pause_channel(channel)
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    # 休止中 (UTC 23:50 = JST 08:50) に開始し、窓オープン (UTC 00:00) の 20 分後に終わる番組。
    # 開始時刻が休止中なので resolve は送出しない = 実際には放送されない。
    Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="休止中に始まる番組 (送出スキップ)",
        start_at=datetime(2026, 1, 1, 23, 50, tzinfo=UTC),
        end_at=datetime(2026, 1, 2, 0, 20, tzinfo=UTC),
        live_source=ls,
    )
    # 窓オープンで発行済みの送出中フィラー (a0 を残り 30 分から再開したもの)。
    win_open = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    _onair_filler(channel, a0, win_open, in_ms=1_800_000, out_ms=3_600_000)

    # 窓オープン後 (00:25) の周期 resolve。
    resolve(
        channel, datetime(2026, 1, 2, 0, 25, tzinfo=UTC), datetime(2026, 1, 2, 1, 30, tzinfo=UTC)
    )

    live = list(
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_FILLER)
        .exclude(status=PlayoutStatus.CANCELLED)
        .order_by("scheduled_at")
    )
    # 送出中の a0 (残り 30 分) は奪われず、00:30 に a1、01:00 に a0 と鎖状継続する。
    assert [e.scheduled_at for e in live] == [
        win_open,
        datetime(2026, 1, 2, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 2, 1, 0, tzinfo=UTC),
    ]
    assert live[1].asset_id == a1.id
    assert live[1].params["pl_idx"] == 1
    assert live[2].asset_id == a0.id


# 本番 ch1 と同じ編成パラメータ (15 クリップ / 1 日 2 窓) での確認。
# 短いプレイリストでは露見しない index・剰余まわりの取り違えを防ぐ。
_PROD_WINDOWS = [{"start": "06:00", "end": "10:00"}, {"start": "16:00", "end": "24:00"}]
_PROD_DURS_MS = [
    3_689_500,
    3_385_100,
    1_751_200,
    219_200,
    222_500,
    3_222_100,
    4_038_600,
    5_153_700,  # idx 7
    9_606_400,
    5_810_200,
    3_562_600,
    6_763_100,
    3_626_600,
    4_171_100,
    4_490_100,
]


def test_resolve_filler_resume_matches_real_channel_geometry(channel):
    """本番と同じ 15 クリップ / 2 窓/日 の編成でも、休止をまたいだ再開位置が実放送と一致する。

    idx7 のクリップを 14:30Z (放送中) に開始 → 15:00Z の窓クローズまで 30 分再生 → 6 時間の休止 →
    21:00Z の窓オープンで in_ms=1,800,000 から再開 → 21:30Z に 3 分番組が割り込み → 番組明けは
    1,800,000 + 1,800,000 = 3,600,000 から再開する。
    """
    pl = FillerPlaylist.objects.create(name="prod-like")
    assets = []
    for i, dur in enumerate(_PROD_DURS_MS):
        a = Asset.objects.create(
            kind=AssetKind.FILLER,
            title=f"prod{i}",
            duration_ms=dur,
            r2_key=f"mezzanine/filler/prod{i}.mp4",
            normalize_status=NormalizeStatus.READY,
        )
        FillerItem.objects.create(filler_playlist=pl, seq=i + 1, asset=a)
        assets.append(a)
    channel.default_filler = pl
    channel.broadcast_windows = _PROD_WINDOWS  # JST 06-10 / 16-24 = UTC 21-01 / 07-15
    channel.save(update_fields=["default_filler", "broadcast_windows"])

    _onair_filler(channel, assets[7], datetime(2026, 7, 26, 14, 30, tzinfo=UTC), pl_idx=7)
    ls = LiveSource.objects.create(name="studio", rtmp_app="live", rtmp_key="k")
    prog_end = datetime(2026, 7, 26, 21, 33, tzinfo=UTC)
    Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="休止明けの番組",
        start_at=datetime(2026, 7, 26, 21, 30, tzinfo=UTC),
        end_at=prog_end,
        live_source=ls,
    )

    # t0 = 休止中。窓オープン (21:00Z) は発行ホライズン (+3h) の内側。
    resolve(
        channel, datetime(2026, 7, 26, 20, 0, tzinfo=UTC), datetime(2026, 7, 26, 23, 0, tzinfo=UTC)
    )

    win_open = datetime(2026, 7, 26, 21, 0, tzinfo=UTC)
    at_open = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_FILLER, scheduled_at=win_open
    )
    assert at_open.params["pl_idx"] == 7
    assert at_open.params["in_ms"] == 1_800_000

    after = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_FILLER, scheduled_at=prog_end
    )
    assert after.params["pl_idx"] == 7
    assert after.params["in_ms"] == 3_600_000
    assert after.params["out_ms"] == _PROD_DURS_MS[7]

    # 休止時間帯には 1 件も発行しない。
    assert not [
        e
        for e in PlayoutEvent.objects.filter(
            channel=channel, action=PlayoutAction.PLAY_FILLER
        ).exclude(status=PlayoutStatus.CANCELLED)
        if not channel.is_on_air(e.scheduled_at)
    ]
