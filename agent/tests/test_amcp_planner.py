# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""amcp_planner.plan の各 action マッピングを検証 (docs/casparcg.md §2.8 対応表)。"""

from __future__ import annotations

import pytest
from icstv.v1 import playout_pb2

from icstv_agent import amcp_planner

CHANNEL = 1
FPS = 60
SLATE = "slate/please_wait"


def _ev(action: int, params: dict[str, str] | None = None) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=action)
    if params:
        ev.params.update(params)
    return ev


def test_play_asset_with_seek_and_length():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/42", "in_ms": "0", "out_ms": "30000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "asset/42" SEEK 0 LENGTH 1800'
    assert plan.take == "PLAY 1-10"
    assert plan.is_immediate is False


def test_play_asset_resumes_at_mid_point():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/42", "in_ms": "60000", "out_ms": "90000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "asset/42" SEEK 3600 LENGTH 1800'


def test_play_asset_falls_back_to_asset_id():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"asset_id": "99", "in_ms": "0", "out_ms": "1000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert '"asset/99"' in (plan.loadbg or "")


def test_play_cm_single():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_CM, {"clip": "cm/1001"})
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/1001"'
    assert plan.take == "PLAY 1-10"


def test_play_filler_loops():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_FILLER, {"clip": "filler/default"})
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "filler/default" LOOP'


def test_play_filler_resumes_at_interruption_offset():
    """実番組による中断からの再開 (in_ms>0) は SEEK/LENGTH を付与する。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
        {"clip": "filler/7", "in_ms": "30000", "out_ms": "60000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "filler/7" LOOP SEEK 1800 LENGTH 1800'


def test_cut_live_constructs_rtmp_url():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
        {"rtmp_app": "live", "rtmp_key": "ch1"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" MIX 15'
    assert plan.take == "PLAY 1-10"


def test_cut_live_with_explicit_url():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
        {"rtmp_url": "rtmp://mediamtx.svc:1935/live/ch1"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "rtmp://mediamtx.svc:1935/live/ch1" MIX 15'


def test_play_slate_is_immediate_no_loadbg():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_SLATE)
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg is None
    assert plan.is_immediate is True
    assert plan.take == 'PLAY 1-90 "slate/please_wait" LOOP'


def test_yt_transition_is_noop_for_agent():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_YT_TRANSITION)
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg is None
    assert plan.take == ""
    assert plan.is_immediate is True


def test_play_cm_bundle_single_clip_has_no_reel():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE, {"clip": "cm/2001"})
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/2001" MIX 15'
    assert plan.take == "PLAY 1-10"
    assert plan.reel == ()


def test_play_cm_bundle_chains_reel_via_auto():
    """先頭は MIX で取り、後続は前 clip の尺だけ待って LOADBG ... AUTO で逐次連結。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        {"clips": "cm/2001:15000,cm/2002:20000,cm/2003:15000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/2001" MIX 15'
    assert plan.take == "PLAY 1-10"
    assert plan.reel == (
        ('LOADBG 1-10 "cm/2002" AUTO', 15000),
        ('LOADBG 1-10 "cm/2003" AUTO', 20000),
    )


def test_play_cm_bundle_appends_live_return_step_for_cm_in():
    """CM IN (return_rtmp_url 付き) は reel 末尾に生復帰の LOADBG ... AUTO を積む (#7 O-B)。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        {
            "clips": "cm/2001:15000,cm/2002:20000",
            "return_rtmp_url": "rtmp://127.0.0.1:1935/live/ch1",
        },
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.reel == (
        ('LOADBG 1-10 "cm/2002" AUTO', 15000),
        # 末尾: 最後の CM (20000ms) の尺だけ待って生を AUTO ロード → CM 終了で本線が生へ戻る
        ('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" AUTO', 20000),
    )


def test_play_vt_with_seek_and_length():
    """生番組内 VT ロール (#25 §6.4): SEEK/LENGTH で clip 再生 + MIX 15 で取る。"""
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_VT)
    ev.play_vt.clip = "asset/500"
    ev.play_vt.in_ms = 0
    ev.play_vt.out_ms = 30000
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "asset/500" SEEK 0 LENGTH 1800 MIX 15'
    assert plan.take == "PLAY 1-10"
    assert plan.is_immediate is False


def test_play_vt_appends_live_return_step():
    """VT 明けは生復帰: reel 末尾に LOADBG ... AUTO (return_rtmp_url 由来) を積む。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_VT,
        {"return_rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"},
    )
    ev.play_vt.clip = "asset/500"
    ev.play_vt.in_ms = 0
    ev.play_vt.out_ms = 30000
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.reel == (('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" AUTO', 30000),)


def test_play_vt_has_no_cg():
    """VT ロールはバンパー等の CG を出さない (ユーザー確定判断、#25 §6.4)。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_VT,
        {"cg_lbar_add": "1", "cg_cm_in": "1", "cg_sponsor": "スポンサー"},
    )
    ev.play_vt.clip = "asset/500"
    ev.play_vt.in_ms = 0
    ev.play_vt.out_ms = 30000
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.cg == ()


def test_unknown_action_raises():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_UNSPECIFIED)
    with pytest.raises(ValueError):
        amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)


def test_slate_command_helper():
    cmd = amcp_planner.slate_command(channel=CHANNEL, slate_clip=SLATE)
    assert cmd == 'PLAY 1-90 "slate/please_wait" LOOP'


def test_clear_slate_action():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE)
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg is None and plan.is_immediate
    assert plan.take == "CLEAR 1-90"


def test_clear_slate_command_helper():
    assert amcp_planner.clear_slate_command(channel=CHANNEL) == "CLEAR 1-90"


def test_yt_mirror_filler_command_helper():
    cmd = amcp_planner.yt_mirror_filler_command(2, "filler/site_only_default")
    assert cmd == 'PLAY 2-10 "filler/site_only_default" LOOP'


def test_yt_mirror_route_command_helper():
    """route:// はチャンネル全体合成なので layer 番号を付けてはいけない (docs §7.7)。"""
    cmd = amcp_planner.yt_mirror_route_command(2, CHANNEL)
    assert cmd == "PLAY 2-10 route://1"


def test_play_asset_emits_sponsor_credit_cg():
    """番組頭 PLAY_ASSET に cg_sponsor があれば CG 1-50 へ提供クレジットを派生 (casparcg.md §3.5)。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {
            "clip": "asset/42",
            "in_ms": "0",
            "out_ms": "1000",
            "cg_sponsor": "トヨペット、〇〇商事",
            "cg_template": "credit/sponsor",
        },
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert len(plan.cg) == 1
    cmd = plan.cg[0]
    assert cmd.startswith('CG 1-50 ADD 0 "credit/sponsor" 1 ')  # play-on-load=1
    assert "トヨペット、〇〇商事" in cmd
    assert '\\"sponsor\\"' in cmd  # JSON はダブルクオートをエスケープして注入


def test_play_asset_without_sponsor_has_no_cg():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/42", "in_ms": "0", "out_ms": "1000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.cg == ()  # 提供番組でなければ CG なし


# ---- typed payload からの読み取り (gRPC 契約の型付け) ----


def test_play_asset_from_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = "asset/9"
    ev.play_asset.in_ms = 60000
    ev.play_asset.out_ms = 90000
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "asset/9" SEEK 3600 LENGTH 1800'


def test_play_cm_from_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM)
    ev.play_cm.clip = "cm/55"
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/55"'


def test_play_filler_from_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    ev.play_filler.clip = "filler/night"
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "filler/night" LOOP'


def test_play_filler_resumes_at_interruption_offset_from_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    ev.play_filler.clip = "filler/7"
    ev.play_filler.in_ms = 30000
    ev.play_filler.out_ms = 60000
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "filler/7" LOOP SEEK 1800 LENGTH 1800'


def test_cut_live_from_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_CUT_LIVE)
    ev.cut_live.rtmp_url = "rtmp://mtx:1935/live/ch1"
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "rtmp://mtx:1935/live/ch1" MIX 15'


def test_bundle_from_payload_items():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE)
    ev.play_cm_bundle.items.add(clip="cm/1", duration_ms=15000)
    ev.play_cm_bundle.items.add(clip="cm/2", duration_ms=20000)
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/1" MIX 15'
    assert plan.reel == (('LOADBG 1-10 "cm/2" AUTO', 15000),)


def test_clip_of_reads_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM)
    ev.play_cm.clip = "cm/7"
    assert amcp_planner.clip_of(ev) == "cm/7"
    bundle = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE)
    bundle.play_cm_bundle.items.add(clip="cm/1", duration_ms=15000)
    assert amcp_planner.clip_of(bundle) == ""  # bundle は prefetch loop 対象外


def test_clip_of_reads_play_vt_payload():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_VT)
    ev.play_vt.clip = "asset/500"
    assert amcp_planner.clip_of(ev) == "asset/500"


def test_return_rtmp_step_helper():
    """_return_rtmp_step: return_rtmp_url 無しは None、有れば (LOADBG ... AUTO, hold_ms)。"""
    assert amcp_planner._return_rtmp_step("1-10", {}, 15000) is None
    assert amcp_planner._return_rtmp_step(
        "1-10", {"return_rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"}, 15000
    ) == ('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" AUTO', 15000)


def test_return_rtmp_step_falls_back_to_app_key():
    """通常運用の emit_live は return_rtmp_url ではなく return_rtmp_app/return_rtmp_key
    しか渡さない (server 側は agent ローカルの MediaMTX host を知らない設計) —
    その場合も同じ組み立てで LOADBG ... AUTO が組まれることを確認 (回帰: この fallback が
    無いと CM/VT 明けの自動本線復帰が通常運用で一切発火しない)。"""
    assert amcp_planner._return_rtmp_step(
        "1-10", {"return_rtmp_app": "live", "return_rtmp_key": "ch1"}, 15000
    ) == ('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" AUTO', 15000)
    # url があれば app/key より優先
    assert amcp_planner._return_rtmp_step(
        "1-10",
        {
            "return_rtmp_url": "rtmp://mediamtx.svc:1935/live/ch1",
            "return_rtmp_app": "ignored",
            "return_rtmp_key": "ignored",
        },
        15000,
    ) == ('LOADBG 1-10 "rtmp://mediamtx.svc:1935/live/ch1" AUTO', 15000)
    # app/key の片方だけでは組み立てない
    assert amcp_planner._return_rtmp_step("1-10", {"return_rtmp_app": "live"}, 15000) is None


def test_params_fallback_still_works():
    # payload 未設定なら従来どおり params から読む (移行/補助経路)
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_CM, {"clip": "cm/legacy"})
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg == 'LOADBG 1-10 "cm/legacy"'


# ---- CG オーバーレイ配線 (casparcg.md §3.4) ----


def test_play_asset_head_emits_bumper_load_not_lbar():
    # Lバー(1-30)/予告(1-40) は OverlayManager 管轄に移動。plan().cg はバンパー(1-20)のみ。
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {
            "clip": "asset/1",
            "in_ms": "0",
            "out_ms": "1000",
            "cg_lbar_add": "1",
            "cg_title": "夜のニュース",
            "cg_channel": "ICS-TV",
        },
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert 'CG 1-20 ADD 0 "bumper/cm-in" 0' in plan.cg  # バンパー背面ロード
    assert not any("1-30" in c for c in plan.cg)  # Lバーは cg に乗らない


def test_play_cm_cm_in_emits_bumper_play_only():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_CM, {"clip": "cm/1", "cg_cm_in": "1"})
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert "CG 1-20 PLAY 0" in plan.cg  # バンパー イン
    assert not any("1-30" in c for c in plan.cg)  # Lバー退避は OverlayManager


# ---- 黒フェード (dip to black) MIXER ビルダ (フィラー↔番組境界用) ----


def test_ms_to_frames_is_fps_linked():
    assert amcp_planner.ms_to_frames(500, 60) == 30  # 60fps で 0.5s = 30 フレーム
    assert amcp_planner.ms_to_frames(500, 30) == 15  # 30fps で 0.5s = 15 フレーム


def test_main_dip_cmds_fade_out_to_black_and_silence():
    cmds = amcp_planner.main_dip_cmds(CHANNEL, level=0.0, frames=30)
    assert cmds == (
        "MIXER 1-10 OPACITY 0.0 30 linear",  # 映像を黒(下地)へ
        "MIXER 1-10 VOLUME 0.0 30 linear",  # 音声を無音へ
    )


def test_main_dip_cmds_fade_in_to_normal():
    cmds = amcp_planner.main_dip_cmds(CHANNEL, level=1.0, frames=30)
    assert cmds == (
        "MIXER 1-10 OPACITY 1.0 30 linear",
        "MIXER 1-10 VOLUME 1.0 30 linear",
    )


def test_main_reset_levels_is_instant_no_tween():
    cmds = amcp_planner.main_reset_levels_cmds(CHANNEL)
    assert cmds == ("MIXER 1-10 OPACITY 1.0", "MIXER 1-10 VOLUME 1.0")


def test_play_asset_cm_out_emits_bumper_stop_only():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/1", "in_ms": "0", "out_ms": "1000", "cg_cm_out": "1"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert "CG 1-20 STOP 0" in plan.cg  # バンパー アウト
    assert not any("1-30" in c for c in plan.cg)  # Lバー復帰は OverlayManager


def test_no_cg_hints_means_no_overlay():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/1", "in_ms": "0", "out_ms": "1000"},
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.cg == ()


# ---- OVERLAY_OP: 手動グラフィック (#18 §B) ----


def test_overlay_op_show_text_is_immediate_cg():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_OVERLAY_OP,
        {
            "overlay_layer": "40",
            "overlay_op": "show",
            "overlay_kind": "text",
            "overlay_data": '{"text":"速報"}',
        },
    )
    plan = amcp_planner.plan(ev, channel=CHANNEL, fps=FPS, slate_clip=SLATE)
    assert plan.loadbg is None and plan.is_immediate  # 本線に触れず即時
    assert plan.take.startswith('CG 1-40 ADD 0 "telop/breaking" 1')
    assert "速報" in plan.take


def test_overlay_op_cmd_variants():
    f = amcp_planner.overlay_op_cmd
    assert (
        f(1, {"overlay_layer": "40", "overlay_op": "hide", "overlay_kind": "text"})
        == "CG 1-40 STOP 0"
    )
    assert f(1, {"overlay_layer": "45", "overlay_op": "clear"}) == "CLEAR 1-45"
    assert (
        f(
            1,
            {
                "overlay_layer": "45",
                "overlay_op": "show",
                "overlay_kind": "video",
                "overlay_clip": "graphic/1",
                "overlay_loop": "1",
            },
        )
        == 'PLAY 1-45 "graphic/1" LOOP'
    )


# ---- retake_plan: casparcg 再接続時の現行イベント貼り直し (#7) ----


def test_retake_play_asset_loops_clip():
    """有限尺 asset でも再起動黒落ち回避を優先し PLAY ... LOOP で貼り直す (LOADBG なし)。"""
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        {"clip": "asset/42", "in_ms": "60000", "out_ms": "90000"},
    )
    assert amcp_planner.retake_plan(ev, channel=CHANNEL) == ('PLAY 1-10 "asset/42" LOOP',)


def test_retake_play_asset_uses_typed_payload_clip():
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = "asset/typed"
    assert amcp_planner.retake_plan(ev, channel=CHANNEL) == ('PLAY 1-10 "asset/typed" LOOP',)


def test_retake_play_filler_loops_clip():
    ev = _ev(playout_pb2.PLAYOUT_ACTION_PLAY_FILLER, {"clip": "filler/loop1"})
    assert amcp_planner.retake_plan(ev, channel=CHANNEL) == ('PLAY 1-10 "filler/loop1" LOOP',)


def test_retake_cut_live_reingests_without_loop():
    """生は LOOP/SEEK 不可 → LOADBG ... MIX → PLAY で再 ingest。"""
    ev = _ev(playout_pb2.PLAYOUT_ACTION_CUT_LIVE, {"rtmp_url": "rtmp://x/live/key"})
    assert amcp_planner.retake_plan(ev, channel=CHANNEL) == (
        'LOADBG 1-10 "rtmp://x/live/key" MIX 15',
        "PLAY 1-10",
    )


def test_retake_cut_live_builds_url_from_app_key():
    ev = _ev(
        playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
        {"rtmp_app": "studio", "rtmp_key": "abc"},
    )
    assert amcp_planner.retake_plan(ev, channel=CHANNEL) == (
        'LOADBG 1-10 "rtmp://127.0.0.1:1935/studio/abc" MIX 15',
        "PLAY 1-10",
    )


def test_retake_transient_actions_return_empty():
    """CM/bundle/slate/yt は本線の持続状態ではないので貼り直さない (空)。"""
    for action in (
        playout_pb2.PLAYOUT_ACTION_PLAY_CM,
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        playout_pb2.PLAYOUT_ACTION_PLAY_SLATE,
        playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE,
        playout_pb2.PLAYOUT_ACTION_YT_TRANSITION,
    ):
        assert amcp_planner.retake_plan(_ev(action), channel=CHANNEL) == ()
