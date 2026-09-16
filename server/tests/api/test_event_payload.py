# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""gRPC 契約の typed payload 化: _event_to_proto が action 別 payload を埋める。"""

from __future__ import annotations

from django.utils import timezone

from playout.grpc_service import _event_to_proto
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus


def _ev(channel, action, params):
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=action,
        status=PlayoutStatus.SCHEDULED,
        params=params,
    )


def test_play_asset_payload(channel):
    ev = _ev(
        channel,
        PlayoutAction.PLAY_ASSET,
        {"clip": "asset/42", "in_ms": 0, "out_ms": 30000, "r2_key": "mezzanine/program/42.mp4"},
    )
    msg = _event_to_proto(ev)
    assert msg.WhichOneof("payload") == "play_asset"
    assert msg.play_asset.clip == "asset/42"
    assert msg.play_asset.in_ms == 0
    assert msg.play_asset.out_ms == 30000
    assert msg.play_asset.r2_key == "mezzanine/program/42.mp4"


def test_play_cm_payload(channel):
    ev = _ev(channel, PlayoutAction.PLAY_CM, {"clip": "cm/7", "r2_key": "mezzanine/cm/7.mp4"})
    msg = _event_to_proto(ev)
    assert msg.WhichOneof("payload") == "play_cm"
    assert msg.play_cm.clip == "cm/7"
    assert msg.play_cm.r2_key == "mezzanine/cm/7.mp4"


def test_play_cm_bundle_items(channel):
    ev = _ev(
        channel,
        PlayoutAction.PLAY_CM_BUNDLE,
        {"clips": "cm/2001:15000,cm/2002:20000", "interrupt": True},
    )
    msg = _event_to_proto(ev)
    assert msg.WhichOneof("payload") == "play_cm_bundle"
    items = msg.play_cm_bundle.items
    assert [(it.clip, it.duration_ms) for it in items] == [("cm/2001", 15000), ("cm/2002", 20000)]


def test_cut_live_payload_with_explicit_url(channel):
    ev = _ev(channel, PlayoutAction.CUT_LIVE, {"rtmp_url": "rtmp://mtx:1935/live/ch1"})
    msg = _event_to_proto(ev)
    assert msg.cut_live.rtmp_url == "rtmp://mtx:1935/live/ch1"


def test_cut_live_payload_empty_when_only_app_key(channel):
    # host は送出ノード固有のため server は組み立てない (agent が params から組む)
    ev = _ev(channel, PlayoutAction.CUT_LIVE, {"rtmp_app": "live", "rtmp_key": "ch1"})
    msg = _event_to_proto(ev)
    assert msg.cut_live.rtmp_url == ""
    assert msg.params["rtmp_app"] == "live"  # params フォールバックは残る


def test_play_filler_payload(channel):
    ev = _ev(channel, PlayoutAction.PLAY_FILLER, {"clip": "filler/default", "loop": True})
    msg = _event_to_proto(ev)
    assert msg.WhichOneof("payload") == "play_filler"
    assert msg.play_filler.clip == "filler/default"
    assert msg.play_filler.loop is True
    assert msg.play_filler.in_ms == 0
    assert msg.play_filler.out_ms == 0


def test_play_filler_payload_carries_resume_offset(channel):
    """実番組による中断からの再開クリップは in_ms/out_ms を typed payload へコピーする。"""
    ev = _ev(
        channel,
        PlayoutAction.PLAY_FILLER,
        {"clip": "filler/7", "loop": True, "in_ms": 30000, "out_ms": 60000},
    )
    msg = _event_to_proto(ev)
    assert msg.play_filler.in_ms == 30000
    assert msg.play_filler.out_ms == 60000


def test_params_still_carries_auxiliary(channel):
    # cg_sponsor 等の補助は payload には載らず params に残る
    ev = _ev(
        channel,
        PlayoutAction.PLAY_ASSET,
        {"clip": "asset/1", "in_ms": 0, "out_ms": 1000, "cg_sponsor": "提供主A"},
    )
    msg = _event_to_proto(ev)
    assert msg.params["cg_sponsor"] == "提供主A"


def test_overlay_op_maps_action_and_carries_params(channel):
    from icstv.v1 import playout_pb2

    ev = _ev(
        channel,
        PlayoutAction.OVERLAY_OP,
        {"overlay_layer": "40", "overlay_op": "show", "overlay_kind": "text"},
    )
    msg = _event_to_proto(ev)
    assert msg.action == playout_pb2.PLAYOUT_ACTION_OVERLAY_OP  # UNSPECIFIED 化しない
    assert msg.params["overlay_layer"] == "40"
    assert msg.params["overlay_op"] == "show"
