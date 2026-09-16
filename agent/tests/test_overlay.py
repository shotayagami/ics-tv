# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OverlayManager (Lバー 1-30 / 次番組予告 1-35) の状態調停テスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from icstv.v1 import playout_pb2

from icstv_agent.overlay import OverlayManager

ASSET = playout_pb2.PLAYOUT_ACTION_PLAY_ASSET
FILLER = playout_pb2.PLAYOUT_ACTION_PLAY_FILLER
CM = playout_pb2.PLAYOUT_ACTION_PLAY_CM
OVERLAY_OP = playout_pb2.PLAYOUT_ACTION_OVERLAY_OP
PLAY_VT = playout_pb2.PLAYOUT_ACTION_PLAY_VT


class FakeCaspar:
    def __init__(self) -> None:
        self.cmds: list[str] = []

    async def amcp(self, command: str):
        self.cmds.append(command)
        return None


def _filler_params(title="", next_title="次の番組", next_start="2026-06-14T21:00:00+09:00"):
    return {
        "cg_title": title,
        "cg_channel": "ICS-TV",
        "cg_preview_show": "1",
        "cg_next_title": next_title,
        "cg_next_start": next_start,
    }


def test_filler_shows_lbar_blank_and_preview():
    c = FakeCaspar()
    om = OverlayManager(c, channel=1)
    asyncio.run(om.on_event(FILLER, _filler_params()))
    # Lバー: ADD (ロゴ=チャンネル名/時計, タイトル空) + PLAY
    assert any('CG 1-30 ADD 0 "lbar/standard" 1' in x for x in c.cmds)
    assert "CG 1-30 PLAY 0" in c.cmds
    # 予告: ADD + PLAY (次番組情報)
    assert any('CG 1-35 ADD 0 "preview/next" 0' in x for x in c.cmds)
    assert "CG 1-35 PLAY 0" in c.cmds
    assert any("次の番組" in x for x in c.cmds)


def test_lbar_updates_title_without_readd():
    c = FakeCaspar()
    om = OverlayManager(c, channel=1)
    asyncio.run(om.on_event(ASSET, {"cg_title": "番組A", "cg_channel": "ICS-TV"}))
    c.cmds.clear()
    asyncio.run(om.on_event(ASSET, {"cg_title": "番組B", "cg_channel": "ICS-TV"}))
    # 2 回目はチラつき防止のため ADD せず UPDATE でタイトル差し替え
    assert not any("ADD" in x and "1-30" in x for x in c.cmds)
    assert any("CG 1-30 UPDATE 0" in x and "番組B" in x for x in c.cmds)


def test_cm_hides_lbar_and_preview():
    c = FakeCaspar()
    om = OverlayManager(c, channel=1)
    asyncio.run(om.on_event(FILLER, _filler_params()))
    c.cmds.clear()
    asyncio.run(om.on_event(CM, {"clip": "cm/1", "cg_cm_in": "1"}))
    assert "CG 1-30 STOP 0" in c.cmds  # Lバー退避
    assert "CG 1-35 STOP 0" in c.cmds  # 予告退避


def test_program_preview_hidden_at_head_then_timer():
    """番組頭では予告は出ず (Lバーは出る)、cg_preview_at 到来で出る。"""
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)
    future = {
        "cg_title": "番組A",
        "cg_channel": "ICS-TV",
        "cg_preview_at": (now + timedelta(hours=1)).isoformat(),
        "cg_next_title": "次の番組",
        "cg_next_start": "2026-06-14T21:00:00+09:00",
    }

    async def drive_future():
        await om.on_event(ASSET, future)
        await asyncio.sleep(0.02)
        om._cancel_timer()

    asyncio.run(drive_future())
    assert any("CG 1-30 ADD" in x for x in c.cmds)  # Lバーは出る
    assert "CG 1-35 PLAY 0" not in c.cmds  # 予告はまだ出ない

    # cg_preview_at が過去 (残り3分突入済) → 即時表示
    c2 = FakeCaspar()
    om2 = OverlayManager(c2, channel=1, clock=lambda: now)
    due = {**future, "cg_preview_at": (now - timedelta(seconds=1)).isoformat()}

    async def drive_due():
        await om2.on_event(ASSET, due)
        await asyncio.sleep(0.02)

    asyncio.run(drive_due())
    assert any('CG 1-35 ADD 0 "preview/next"' in x for x in c2.cmds)
    assert "CG 1-35 PLAY 0" in c2.cmds


def test_play_vt_keeps_lbar_and_preview():
    """PLAY_VT (生番組内 VT ロール) は本線コンテンツ扱い: L バー/予告を退避しない (#25 §6.6)。

    直前 CUT_LIVE の cg_* params をそのまま引き継ぐ前提 (server 側 fire_vt_asset)。ここでは
    その引き継ぎ後の params を渡し、L バーが CM のように STOP されず、渡したタイトルで
    _show_lbar されることを確認する (誤引っ込みバグの直接修正)。
    """
    c = FakeCaspar()
    om = OverlayManager(c, channel=1)
    asyncio.run(om.on_event(PLAY_VT, {"cg_title": "夜のニュース", "cg_channel": "ICS-TV"}))
    assert not any("1-30 STOP" in x for x in c.cmds)  # L バーは退避されない
    assert any('CG 1-30 ADD 0 "lbar/standard" 1' in x for x in c.cmds)
    assert any("夜のニュース" in x for x in c.cmds)  # 引き継いだタイトルで表示
    assert "CG 1-30 PLAY 0" in c.cmds


def test_restore_readds_after_restart():
    c = FakeCaspar()
    om = OverlayManager(c, channel=1)
    asyncio.run(om.on_event(FILLER, _filler_params()))
    c.cmds.clear()
    # casparcg 再起動相当: restore で CG を再 ADD して復帰
    asyncio.run(om.restore(FILLER, _filler_params()))
    assert any("CG 1-30 ADD" in x for x in c.cmds)
    assert any("CG 1-35 ADD" in x for x in c.cmds)


# ---- cg_cues: 番組/フィラー自動グラフィックセット (#18 §C) ----


def _cues(now):
    import json

    return json.dumps(
        [
            {
                "layer": 45,
                "kind": "graphic",
                "data": {"elements": [{"text": "提供"}]},
                "template": "",
                "show_at": (now - timedelta(seconds=1)).isoformat(),  # 既に窓内
                "hide_at": (now + timedelta(minutes=5)).isoformat(),
            }
        ]
    )


def test_cg_cues_shows_in_window():
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _cues(now)})
        await asyncio.sleep(0.02)

    asyncio.run(drive())
    assert any('CG 1-45 ADD 0 "graphic/freeform" 1' in x for x in c.cmds)
    assert any("提供" in x for x in c.cmds)


def test_cg_cues_cleared_on_cm():
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _cues(now)})
        await asyncio.sleep(0.02)
        c.cmds.clear()
        await om.on_event(CM, {"cg_cm_in": "1"})  # CM → 自動グラフィックも退避

    asyncio.run(drive())
    assert "CLEAR 1-45" in c.cmds


# ---- 朝・夕の左上時計 (daypart corner clock, layer 1-36) ----


def _clock_cue(now, *, show_offset_s=-1, hide_offset_h=4):
    import json

    return json.dumps(
        [
            {
                "layer": 36,
                "kind": "graphic",
                "template": "clock/corner",
                "data": {"seconds": False, "date": False},
                "show_at": (now + timedelta(seconds=show_offset_s)).isoformat(),
                "hide_at": (now + timedelta(hours=hide_offset_h)).isoformat(),
            }
        ]
    )


def test_clock_cue_shows_in_window():
    """窓内 (show_at 過去 / hide_at 未来) の時計 cue は clock/corner を即 ADD する。"""
    now = datetime(2026, 6, 24, 6, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(
            ASSET,
            {
                "cg_title": "朝の番組",
                "cg_channel": "ICS-TV",
                "cg_cues": _clock_cue(now),
            },
        )
        await asyncio.sleep(0.02)

    asyncio.run(drive())
    assert any('CG 1-36 ADD 0 "clock/corner" 1' in x for x in c.cmds)


def test_clock_cue_not_shown_before_window():
    """show_at が未来の時計 cue は時刻到来までは出さない (タイマー予約のみ)。"""
    now = datetime(2026, 6, 24, 3, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(
            ASSET,
            {
                "cg_title": "x",
                "cg_channel": "ICS-TV",
                "cg_cues": _clock_cue(now, show_offset_s=3600),
            },
        )
        await asyncio.sleep(0.02)
        om._cancel_cue_timers()

    asyncio.run(drive())
    assert not any("CG 1-36 ADD" in x for x in c.cmds)


def test_clock_cue_cleared_on_cm():
    """CM 中は時計も退避 (CLEAR)。"""
    now = datetime(2026, 6, 24, 6, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(
            ASSET,
            {
                "cg_title": "朝の番組",
                "cg_channel": "ICS-TV",
                "cg_cues": _clock_cue(now),
            },
        )
        await asyncio.sleep(0.02)
        c.cmds.clear()
        await om.on_event(CM, {"cg_cm_in": "1"})

    asyncio.run(drive())
    assert "CLEAR 1-36" in c.cmds


def test_clock_active_hides_lbar_clock_and_offsets_preview():
    """朝夕時計が出ている間は Lバー時計をOFF + 予告を右へ寄せる (適応動作)。"""
    now = datetime(2026, 6, 24, 6, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _clock_cue(now)})
        await asyncio.sleep(0.02)

    asyncio.run(drive())
    assert any(
        "CG 1-30 UPDATE" in x and "clock" in x and "false" in x for x in c.cmds
    )  # Lバー時計OFF
    assert any(
        "CG 1-35 UPDATE" in x and "clockAvoid" in x and "true" in x for x in c.cmds
    )  # 予告 右寄せ


def test_clock_inactive_restores_lbar_clock_and_preview():
    """時計帯を抜ける (窓を過ぎた cue) と Lバー時計/予告を従来表示へ復帰。"""
    now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _clock_cue(now)})
        await asyncio.sleep(0.02)
        c.cmds.clear()
        # 既に過ぎた窓 (show/hide とも過去) → 時計クリア → 復帰
        await om.on_event(
            FILLER,
            {
                **_filler_params(),
                "cg_cues": _clock_cue(now, show_offset_s=-7200, hide_offset_h=-1),
            },
        )
        await asyncio.sleep(0.02)

    asyncio.run(drive())
    assert any(
        "CG 1-30 UPDATE" in x and "clock" in x and "true" in x for x in c.cmds
    )  # Lバー時計復帰
    assert any(
        "CG 1-35 UPDATE" in x and "clockAvoid" in x and "false" in x for x in c.cmds
    )  # 予告 通常位置


def test_manual_overlay_op_lets_cue_reclaim_layer():
    """手動 OVERLAY_OP で同一レイヤを消した後、次のフィラー境界で自動 cue が取り戻す。"""
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    c = FakeCaspar()
    om = OverlayManager(c, channel=1, clock=lambda: now)

    async def drive():
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _cues(now)})
        await asyncio.sleep(0.02)
        # 手動 op が layer 45 を clear (op_overlay 由来) → _cue_state[45] 無効化
        await om.on_event(OVERLAY_OP, {"overlay_layer": "45", "overlay_op": "clear"})
        c.cmds.clear()
        # 次フィラー境界: sig 同一でも _cue_state が無いので再 ADD される
        await om.on_event(FILLER, {**_filler_params(), "cg_cues": _cues(now)})
        await asyncio.sleep(0.02)

    asyncio.run(drive())
    assert any('CG 1-45 ADD 0 "graphic/freeform"' in x for x in c.cmds)
