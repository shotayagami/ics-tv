# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""timekeeper WS push (#25 Phase2 §4/D8) のテスト。

`tests/api/test_comments_realtime.py` を雛形にする。channel layer は conftest の
autouse fixture で InMemory に差し替え済み (Redis 非依存)。async は async_to_sync で
ラップしてプレーンな pytest から駆動する (pytest-asyncio 不要)。

構成:
1. consumer が group message をクライアントへリレーすること (基本の relay テスト)。
2. core.ops_views の 8 操作 (op_cm_in/op_cm_return/op_cut_live_return/op_extend/
   op_shorten/op_cm_now/op_roll_vt/op_skip_cue) が HTTP 経由で playout.update を配信すること。
3. playout.grpc_service.apply_result の LiveCue AIRED 反映 (Batch B) も配信すること。
4. broadcast_playout_update の best-effort 性 (例外を握りつぶし呼び出し元を壊さない)。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from core.consumers import broadcast_playout_update, playout_group
from core.models import LiveSource
from core.routing import websocket_urlpatterns
from medialib.models import (
    Asset,
    AssetKind,
    CmBundle,
    CmBundleItem,
    CmCreative,
    CmGrid,
    NormalizeStatus,
)
from playout.grpc_service import apply_result
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown, Program, ProgramType

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _url(slug: str, suffix: str) -> str:
    return f"/ops/ch/{slug}/{suffix}"


def _expect_broadcast(channel):
    """probe を group に参加させ、後で layer.receive で playout.update を検証できるようにする。"""
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(playout_group(channel.slug), "probe")
    return layer


def _assert_playout_update_received(layer) -> None:
    msg = async_to_sync(layer.receive)("probe")
    assert msg["type"] == "playout.update"


# ---- 1. consumer relay (test_comments_realtime.test_consumer_relays_group_message 雛形) ----
#
# staff 限定 (2026-07-04 レビュー指摘): PlayoutConsumer.connect() は config.asgi の
# AuthMiddlewareStack が解決する scope["user"] を見て is_staff を検査する。この
# WebsocketCommunicator は AuthMiddlewareStack を経由しない生の URLRouter なので、
# connect() 前に scope["user"] を直接差し込んで認証済みをシミュレートする
# (Channels のテストでの定番手法。scope は WebsocketCommunicator.__init__ で構築され
# connect() 呼び出し時までミュータブルに参照される)。


def test_consumer_relays_group_message(db, staff_user):
    """consumer が接続→group参加し、group_send された playout.update をクライアントへ転送する。"""

    async def scenario():
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), "/ws/playout/ch1/")
        comm.scope["user"] = staff_user
        connected, _ = await comm.connect()
        assert connected
        layer = get_channel_layer()
        await layer.group_send(playout_group("ch1"), {"type": "playout.update"})
        msg = await comm.receive_json_from()
        assert msg["type"] == "playout.update"
        await comm.disconnect()

    async_to_sync(scenario)()


def test_consumer_rejects_anonymous_connection(db):
    """staff_member_required 相当のガード: 未認証の接続は 4003 で拒否され、
    group にも加わらない (2026-07-04 レビュー指摘の修正)。"""
    from django.contrib.auth.models import AnonymousUser

    async def scenario():
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), "/ws/playout/ch1/")
        comm.scope["user"] = AnonymousUser()
        connected, code = await comm.connect()
        assert connected is False
        assert code == 4003

    async_to_sync(scenario)()


def test_consumer_rejects_non_staff_authenticated_user(db, django_user_model):
    """ログイン済みだが is_staff=False の一般ユーザも拒否される。"""

    async def scenario():
        user = django_user_model.objects.create_user(username="rank_and_file", password="x")
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), "/ws/playout/ch1/")
        comm.scope["user"] = user
        connected, code = await comm.connect()
        assert connected is False
        assert code == 4003

    async_to_sync(scenario)()


def test_consumer_rejects_connection_with_no_user_in_scope(db):
    """AuthMiddlewareStack を経由しない (scope に "user" キー自体が無い) 接続も安全側に倒す。"""

    async def scenario():
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), "/ws/playout/ch1/")
        connected, code = await comm.connect()
        assert connected is False
        assert code == 4003

    async_to_sync(scenario)()


# ---- 2. core.ops_views の各操作が playout.update を配信すること ----


def _cm_bundle(name="bundleA", durations=(15_000,)):
    bundle = CmBundle.objects.create(name=name)
    for i, dur in enumerate(durations):
        a = Asset.objects.create(
            kind=AssetKind.CM,
            title=f"cm{i}",
            duration_ms=dur,
            r2_key=f"mezzanine/cm/{name}-{i}.mp4",
            normalize_status=NormalizeStatus.READY,
        )
        cr = CmCreative.objects.create(asset=a, advertiser="adv", grid=CmGrid.G15)
        CmBundleItem.objects.create(cm_bundle=bundle, seq=i, cm_asset=cr)
    return bundle


def _live_source():
    return LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")


def _live_program(channel, start=BASE, end=None, title="生番組"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end or start + timedelta(hours=1),
        live_source=_live_source(),
    )


def _recorded_program(channel, asset, start, end, title="P"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=end,
        asset=asset,
    )


def _cm_cue(channel, bundle, state=LiveCueState.PENDING, rundown=None):
    rundown = rundown or LiveRundown.objects.create(program=_live_program(channel))
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.CM,
        planned_duration_ms=sum(
            it.cm_asset.asset.duration_ms for it in bundle.items.select_related("cm_asset__asset")
        ),
        cm_bundle=bundle,
        grid="15s",
        state=state,
    )


def _vt_cue(channel, asset, state=LiveCueState.PENDING, rundown=None):
    rundown = rundown or LiveRundown.objects.create(program=_live_program(channel))
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.VT,
        label=asset.title,
        planned_duration_ms=asset.duration_ms,
        asset=asset,
        state=state,
    )


def _vt_asset(title="VTR1", duration_ms=90_000, r2_key="mezzanine/vt/vtr1.mp4"):
    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title=title,
        duration_ms=duration_ms,
        r2_key=r2_key,
        normalize_status=NormalizeStatus.READY,
    )


@pytest.mark.django_db
def test_op_cm_in_broadcasts_playout_update(staff_client, channel):
    bundle = _cm_bundle()
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": bundle.id})
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_cm_return_broadcasts_playout_update(staff_client, channel):
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, "cm-return/"))
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_cut_live_return_broadcasts_playout_update(staff_client, channel):
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, "cut-live-return/"))
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_extend_broadcasts_playout_update(staff_client, channel, asset_ready):
    base = timezone.now() + timedelta(hours=1)
    prog = _recorded_program(channel, asset_ready, base, base + timedelta(hours=1))
    layer = _expect_broadcast(channel)
    res = staff_client.post(
        _url(channel.slug, f"program/{prog.id}/extend/"), {"delta_ms": "600000"}
    )
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_shorten_broadcasts_playout_update(staff_client, channel):
    base = timezone.now() + timedelta(hours=1)
    prog = _live_program(channel, start=base, end=base + timedelta(hours=1))
    layer = _expect_broadcast(channel)
    res = staff_client.post(
        _url(channel.slug, f"program/{prog.id}/shorten/"), {"delta_ms": "600000"}
    )
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_cm_now_broadcasts_playout_update(staff_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/"))
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_roll_vt_broadcasts_playout_update(staff_client, channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset)
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/roll-vt/"))
    assert res.status_code == 200
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_op_skip_cue_broadcasts_playout_update(staff_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    layer = _expect_broadcast(channel)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/skip/"))
    assert res.status_code == 200
    _assert_playout_update_received(layer)


# ---- 3. apply_result (Batch B の LiveCue AIRED 反映) も配信すること ----


def _as_run_event(channel, action, *, params=None) -> PlayoutEvent:
    return PlayoutEvent.objects.create(
        idempotency_key=uuid.uuid4(),
        channel=channel,
        scheduled_at=timezone.now(),
        action=action,
        status=PlayoutStatus.SCHEDULED,
        params=params or {},
    )


def _auto_fire_cue(channel, kind) -> LiveCue:
    rundown = LiveRundown.objects.create(program=_live_program(channel))
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=kind,
        planned_duration_ms=30_000,
        state=LiveCueState.PENDING,
        auto_fire=True,
    )


@pytest.mark.django_db
def test_apply_result_broadcasts_on_live_cue_aired(channel):
    cue = _auto_fire_cue(channel, LiveCueKind.CM)
    ev = _as_run_event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"live_cue_id": cue.id})
    layer = _expect_broadcast(channel)

    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    _assert_playout_update_received(layer)


@pytest.mark.django_db
def test_apply_result_does_not_broadcast_when_cue_already_aired(channel):
    """D5 のガード: 既に AIRED な cue には触れない → その競合パスでは配信もしない
    (手動発火側の broadcast だけで十分、二重送信を避ける実装になっていることの確認)。"""
    cue = _auto_fire_cue(channel, LiveCueKind.CM)
    cue.state = LiveCueState.AIRED
    cue.save(update_fields=["state"])
    ev = _as_run_event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"live_cue_id": cue.id})
    layer = _expect_broadcast(channel)

    apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    async def _receive_with_timeout():
        return await asyncio.wait_for(layer.receive("probe"), timeout=0.2)

    with pytest.raises(TimeoutError):
        async_to_sync(_receive_with_timeout)()


# ---- 4. best-effort: broadcast 失敗が呼び出し元を壊さないこと ----


@pytest.mark.django_db
def test_broadcast_playout_update_swallows_exception(monkeypatch):
    """get_channel_layer が例外を投げても broadcast_playout_update 自体は例外を伝播しない。"""

    def _boom():
        raise RuntimeError("channel layer unavailable")

    monkeypatch.setattr("channels.layers.get_channel_layer", _boom)

    broadcast_playout_update("ch1")  # 例外を投げなければ OK


@pytest.mark.django_db
def test_op_skip_cue_succeeds_even_if_broadcast_fails(monkeypatch, staff_client, channel):
    """WS 配信基盤が壊れていても、cue スキップ自体 (本処理) は成功し 200 を返し続けること。"""

    def _boom():
        raise RuntimeError("channel layer unavailable")

    monkeypatch.setattr("channels.layers.get_channel_layer", _boom)

    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/skip/"))

    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.state == LiveCueState.SKIPPED
