# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""playout_event (proto) → AMCP コマンド列の planner (docs/casparcg.md §2)。

役割:
- action と params から LOADBG コマンドと PLAY コマンドを組み立てる純粋関数
- channel/layer 番号は固定 (本線=10, slate=90 など docs/casparcg.md §1.1)
- 単体テスト可能 (実 CasparCG 不要)

Phase 1:
- play_cm_bundle は reel 全体を扱う。先頭を MIX で取り、後続は LOADBG ... AUTO で
  CasparCG 側に逐次自動連結させる (実発火は dispatch_loop が AmcpPlan.reel を流す)
- yt_transition は agent では no-op (YouTube Data API は server 側 youtube/tasks.py)
- CG (テロップ/Lバー/バンパー) は Phase 1 後半
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from icstv.v1 import playout_pb2

# 固定 layer 割当 (docs/casparcg.md §1.1)
LAYER_MAIN = 10
LAYER_LIVE_PARK = 11
LAYER_BUMPER = 20  # CM バンパー (CM IN/OUT のブリッジ)
LAYER_LBAR = 30  # Lバー / 番組情報 (OverlayManager が常駐管理)
LAYER_PREVIEW = 35  # 次番組予告 (OverlayManager 管理。40=速報予約 §1.1)
LAYER_CLOCK = 36  # 朝・夕の左上時計 (daypart。resolver が cg_cues に合成・kind=graphic)
LAYER_HAZARD_MAP = (
    38  # 津波沿岸/震度 フルスクリーン地図 (kind=graphic・map/*.html。40 の下=速報テロップが前面)
)
LAYER_HAZARD_CORNER = (
    37  # 津波ミニマップ (地図+凡例のみ・画面右下・常時表示。LBAR/CLOCKと同じ常駐系)
)
LAYER_BREAKING = 40  # 速報・割り込みテロップ (手動 op, #18 §B.2)
LAYER_CHIME = 41  # 速報チャイム (音声・kind=video の PLAY/STOP。fire_chime が発火)
LAYER_FREEFORM = 45  # 手動フリーグラフィック (#18 §B.3)
LAYER_SPONSOR = 50  # 提供表示 (スポンサークレジット)
LAYER_SLATE = 90

MEDIAMTX_LOCAL = "rtmp://127.0.0.1:1935"  # Phase 1: MediaMTX 同 host 固定 (docs/overview.md 3.5)

# レイヤ状態パネル (#18 §A) 用の layer→role マップ。heartbeat で INFO を集める対象でもある。
LAYER_ROLES: dict[int, str] = {
    LAYER_MAIN: "main",
    LAYER_BUMPER: "bumper",
    LAYER_LBAR: "lbar",
    LAYER_PREVIEW: "preview",
    LAYER_CLOCK: "clock",
    LAYER_HAZARD_CORNER: "hazard_corner",
    LAYER_HAZARD_MAP: "hazard_map",
    LAYER_BREAKING: "breaking",
    LAYER_FREEFORM: "freeform",
    LAYER_SPONSOR: "sponsor",
    LAYER_SLATE: "slate",
}


@dataclass(frozen=True)
class AmcpPlan:
    """1 event 分の AMCP コマンド列。

    loadbg=None かつ is_immediate=True の場合は LOADBG を経ず take を即時発射する
    (play_slate のような最優先割り込み)。
    take="" の場合は agent 側で何もしない (yt_transition は YouTube Data API で server が処理)。

    reel は CM バンドルの 2 本目以降を逐次連結するための後続ステップ列。
    各要素 (amcp, hold_ms): take 後に amcp (LOADBG ... AUTO) を送り、hold_ms だけ
    待ってから次を送る。hold_ms は「いま前面で再生中の clip の尺」= AUTO 背面ロードに
    使える猶予窓。bundle 以外では常に空 ()。
    """

    loadbg: str | None
    take: str
    is_immediate: bool
    reel: tuple[tuple[str, int], ...] = ()
    # take 後に発射する CG オーバーレイコマンド (提供クレジット等)。本線とは別 video layer。
    cg: tuple[str, ...] = field(default_factory=tuple)


def ms_to_frames(ms: int, fps: int) -> int:
    return round(ms * fps / 1000)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _quote(s: str) -> str:
    return '"' + _escape(s) + '"'


def _parse_reel(params: dict[str, str]) -> list[tuple[str, int]]:
    """bundle の clip 列を [(clip, duration_ms), ...] に展開。

    contract: params["clips"] = "cm/2001:15000,cm/2002:20000" (clip:尺ms をカンマ区切り)。
    無ければ単一 params["clip"] (尺不明=0) にフォールバック (後続なし)。
    """
    raw = params.get("clips")
    if raw:
        items: list[tuple[str, int]] = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" in tok:
                clip, dur = tok.rsplit(":", 1)
                items.append((clip.strip(), int(dur)))
            else:
                items.append((tok, 0))
        if items:
            return items
    clip = params.get("clip") or f"cm/{params.get('asset_id', '?')}"
    return [(clip, 0)]


def _sponsor_cg(channel: int, params: dict[str, str]) -> tuple[str, ...]:
    """提供クレジット CG コマンド (docs/casparcg.md §3.5)。番組頭イベントの params に
    cg_sponsor (差し込み文言) があれば CG 1-50 へ ADD (play-on-load=1 で即時イン)。

    クレジットは自走アニメ (テンプレ側 play() がイン→保持→アウト) を前提に STOP を伴わない。"""
    text = params.get("cg_sponsor")
    if not text:
        return ()
    template = params.get("cg_template", "credit/sponsor")
    data = _quote(json.dumps({"sponsor": text}, ensure_ascii=False))
    return (f"CG {channel}-{LAYER_SPONSOR} ADD 0 {_quote(template)} 1 {data}",)


def _overlay_cg(channel: int, params: dict[str, str]) -> tuple[str, ...]:
    """as-run params の cg_* ヒントから CG オーバーレイコマンド列を組む (casparcg.md §3.4)。

    バンパー(1-20)と提供クレジット(1-50)を担当する。Lバー(1-30)/次番組予告(1-35)は状態 (常駐/
    タイトル/CM退避/タイマー) を持つため OverlayManager (overlay.py) が別途管理する。
    番組頭(cg_lbar_add): バンパー背面ロード。CM IN(cg_cm_in): バンパー PLAY。CM OUT(cg_cm_out): バンパー STOP。
    """
    n = channel
    cmds: list[str] = []
    if params.get("cg_lbar_add"):
        cmds.append(f'CG {n}-{LAYER_BUMPER} ADD 0 "bumper/cm-in" 0')  # 番組頭で背面ロード
    if params.get("cg_cm_in"):
        cmds.append(f"CG {n}-{LAYER_BUMPER} PLAY 0")  # バンパー イン
    if params.get("cg_cm_out"):
        cmds.append(f"CG {n}-{LAYER_BUMPER} STOP 0")  # バンパー アウト
    cmds.extend(_sponsor_cg(channel, params))
    return tuple(cmds)


# ---- Lバー(1-30) / 次番組予告(1-35) の AMCP コマンドビルダ (状態は OverlayManager が保持) ----


def lbar_add_cmd(channel: int, *, title: str, channel_name: str, clock: bool = True) -> str:
    """Lバーを ADD (play-on-load=1 で即イン)。ロゴ=チャンネル名, 時計(任意), タイトル。

    clock=False は朝夕の左上時計(layer36)表示中に Lバー側の時計を消して二重表示を避ける用途
    (OverlayManager が daypart 時計の表示状態に合わせて切替える)。
    """
    data = _quote(
        json.dumps(
            {"channel": channel_name, "clock": clock, "title": title},
            ensure_ascii=False,
        )
    )
    return f'CG {channel}-{LAYER_LBAR} ADD 0 "lbar/standard" 1 {data}'


def lbar_update_cmd(channel: int, *, title: str | None = None, clock: bool | None = None) -> str:
    """Lバーの部分更新。指定したフィールドのみ UPDATE する (template の update は present key のみ反映)。"""
    data: dict[str, object] = {}
    if title is not None:
        data["title"] = title
    if clock is not None:
        data["clock"] = clock
    return f"CG {channel}-{LAYER_LBAR} UPDATE 0 {_quote(json.dumps(data, ensure_ascii=False))}"


def lbar_play_cmd(channel: int) -> str:
    return f"CG {channel}-{LAYER_LBAR} PLAY 0"


def lbar_stop_cmd(channel: int) -> str:
    return f"CG {channel}-{LAYER_LBAR} STOP 0"


def preview_add_cmd(channel: int, *, title: str, start: str, avoid_clock: bool = False) -> str:
    """次番組予告を ADD (play-on-load=0 で背面ロード。表示は PLAY で行う)。

    avoid_clock=True は朝夕の左上時計(layer36)表示中に予告の文言を右へ寄せて時計を避ける用途。
    """
    data = _quote(
        json.dumps(
            {"title": title, "start": start, "clockAvoid": avoid_clock},
            ensure_ascii=False,
        )
    )
    return f'CG {channel}-{LAYER_PREVIEW} ADD 0 "preview/next" 0 {data}'


def preview_update_cmd(channel: int, *, title: str, start: str) -> str:
    data = _quote(json.dumps({"title": title, "start": start}, ensure_ascii=False))
    return f"CG {channel}-{LAYER_PREVIEW} UPDATE 0 {data}"


def preview_avoid_cmd(channel: int, *, avoid: bool) -> str:
    """予告の時計回避(右寄せ)のみを切替える UPDATE (左上時計の表示状態に追従)。"""
    data = _quote(json.dumps({"clockAvoid": avoid}, ensure_ascii=False))
    return f"CG {channel}-{LAYER_PREVIEW} UPDATE 0 {data}"


def preview_play_cmd(channel: int) -> str:
    return f"CG {channel}-{LAYER_PREVIEW} PLAY 0"


def preview_stop_cmd(channel: int) -> str:
    return f"CG {channel}-{LAYER_PREVIEW} STOP 0"


def overlay_op_cmd(channel: int, params: dict[str, str]) -> str:
    """手動グラフィック操作 (OVERLAY_OP) の単一 AMCP コマンドを組む (#18 §B)。

    params: overlay_layer / overlay_op(show|update|hide|clear) / overlay_kind(graphic|video|text) /
            overlay_template / overlay_clip / overlay_data(JSON) / overlay_loop。
    """
    layer = params.get("overlay_layer", "0")
    op = params.get("overlay_op", "show")
    kind = params.get("overlay_kind", "graphic")
    n = f"{channel}-{layer}"
    if op == "clear":
        return f"CLEAR {n}"
    if kind == "video":  # 透過動画は本線同様 PLAY/STOP (FFmpeg producer)
        if op == "hide":
            return f"STOP {n}"
        loop = " LOOP" if params.get("overlay_loop") in ("1", "true", "True") else ""
        return f"PLAY {n} {_quote(params.get('overlay_clip', ''))}{loop}"
    # graphic / text (HTML CG)
    if op == "hide":
        return f"CG {n} STOP 0"
    data = _quote(params.get("overlay_data", "{}"))
    if op == "update":
        return f"CG {n} UPDATE 0 {data}"
    template = params.get("overlay_template") or (
        "telop/breaking" if kind == "text" else "graphic/freeform"
    )
    return f"CG {n} ADD 0 {_quote(template)} 1 {data}"


def clip_of(event: playout_pb2.PlayoutEvent) -> str:
    """typed payload から LOADBG clip を取り出す (prefetch 用)。無ければ ""。"""
    which = event.WhichOneof("payload")
    if which == "play_asset":
        return event.play_asset.clip
    if which == "play_cm":
        return event.play_cm.clip
    if which == "play_filler":
        return event.play_filler.clip
    if which == "play_vt":
        return event.play_vt.clip
    return ""


def _rtmp_url_from(params: dict[str, str], *, prefix: str = "") -> str:
    """rtmp_url (直接値) → rtmp_app/rtmp_key (組み立て) の順で解決する (CUT_LIVE 本線と
    reel 末尾の復帰ステップ (_return_rtmp_step) が共有)。prefix="return_" で
    return_rtmp_url/return_rtmp_app/return_rtmp_key を読む。"""
    rtmp_url = params.get(f"{prefix}rtmp_url")
    if rtmp_url:
        return rtmp_url
    app, key = params.get(f"{prefix}rtmp_app", ""), params.get(f"{prefix}rtmp_key", "")
    if not (app and key):
        return ""
    return f"{MEDIAMTX_LOCAL}/{app}/{key}"


def _return_rtmp_step(main: str, params: dict[str, str], hold_ms: int) -> tuple[str, int] | None:
    """末尾の生復帰ステップ (return_rtmp_url/return_rtmp_app+return_rtmp_key があれば
    LOADBG ... AUTO を組む)。無ければ None。

    CM バンドルの CM IN (docs/operations.md O5) と VT ロール (#25 §6.4) の双方で共有する。
    """
    return_rtmp = _rtmp_url_from(params, prefix="return_")
    if not return_rtmp:
        return None
    return (f"LOADBG {main} {_quote(return_rtmp)} AUTO", hold_ms)


def _reel_from_event(
    event: playout_pb2.PlayoutEvent, params: dict[str, str]
) -> list[tuple[str, int]]:
    """bundle の (clip, 尺ms) 列。typed payload を優先し、無ければ params['clips'] にフォールバック。"""
    items = event.play_cm_bundle.items
    if items:
        return [(it.clip, it.duration_ms) for it in items]
    return _parse_reel(params)


def plan(
    event: playout_pb2.PlayoutEvent,
    *,
    channel: int,
    fps: int,
    slate_clip: str,
) -> AmcpPlan:
    """proto PlayoutEvent → AMCP plan。docs/casparcg.md §2.8 の対応表に従う。"""
    action = event.action
    params = dict(event.params)
    main = f"{channel}-{LAYER_MAIN}"

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_ASSET:
        pa = event.play_asset
        if pa.clip:  # typed payload を一次情報に (clip 有 = payload セット済)
            clip, in_ms, out_ms = pa.clip, pa.in_ms, pa.out_ms
        else:  # params フォールバック
            clip = params.get("clip") or f"asset/{params.get('asset_id', '?')}"
            in_ms = int(params.get("in_ms", "0"))
            out_ms = int(params.get("out_ms", "0"))
        seek = ms_to_frames(in_ms, fps)
        length = ms_to_frames(max(0, out_ms - in_ms), fps)
        loadbg = f"LOADBG {main} {_quote(clip)} SEEK {seek} LENGTH {length}"
        return AmcpPlan(
            loadbg=loadbg,
            take=f"PLAY {main}",
            is_immediate=False,
            cg=_overlay_cg(channel, params),  # Lバー常設/復帰 + 提供
        )

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_CM:
        clip = event.play_cm.clip or params.get("clip") or f"cm/{params.get('asset_id', '?')}"
        loadbg = f"LOADBG {main} {_quote(clip)}"
        return AmcpPlan(
            loadbg=loadbg,
            take=f"PLAY {main}",
            is_immediate=False,
            cg=_overlay_cg(channel, params),  # CM IN: バンパー/Lバー退避
        )

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_FILLER:
        pf = event.play_filler
        clip = pf.clip or params.get("clip") or f"filler/{params.get('filler_playlist_id', '?')}"
        in_ms = pf.in_ms or int(params.get("in_ms", "0") or 0)
        if in_ms:
            # 実番組による中断からの再開 (中断位置から残り尺だけ SEEK/LENGTH で再生)。
            out_ms = pf.out_ms or int(params.get("out_ms", "0") or 0)
            seek = ms_to_frames(in_ms, fps)
            length = ms_to_frames(max(0, out_ms - in_ms), fps)
            loadbg = f"LOADBG {main} {_quote(clip)} LOOP SEEK {seek} LENGTH {length}"
        else:
            loadbg = f"LOADBG {main} {_quote(clip)} LOOP"
        return AmcpPlan(loadbg=loadbg, take=f"PLAY {main}", is_immediate=False)

    if action == playout_pb2.PLAYOUT_ACTION_CUT_LIVE:
        # typed payload (grpc_service が params["rtmp_url"] から詰める) を一次情報に、
        # 無ければ params から同じ解決順で組み立てる (_rtmp_url_from)。
        rtmp_url = event.cut_live.rtmp_url or _rtmp_url_from(params)
        # 生は LOOP/SEEK/LENGTH 不可。短いフェードのみ (docs/casparcg.md §2.5)
        loadbg = f"LOADBG {main} {_quote(rtmp_url)} MIX 15"
        return AmcpPlan(loadbg=loadbg, take=f"PLAY {main}", is_immediate=False)

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_SLATE:
        # docs/casparcg.md §2.7: 最優先割り込み。LOADBG 不要の clip 付き PLAY を slate layer に。
        return AmcpPlan(
            loadbg=None,
            take=f"PLAY {channel}-{LAYER_SLATE} {_quote(slate_clip)} LOOP",
            is_immediate=True,
        )

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE:
        # reel 全体を扱う。先頭は生→CM の短いフェードで取り (MIX 15)、後続は
        # LOADBG ... AUTO で背面に積み、前面 clip 終了時に CasparCG が自動連結する。
        # 各後続ステップの hold_ms = 直前 (= いま前面) clip の尺 (AUTO 投入の猶予窓)。
        reel_items = _reel_from_event(event, params)
        clip0 = reel_items[0][0]
        loadbg = f"LOADBG {main} {_quote(clip0)} MIX 15"
        reel_steps = [
            (f"LOADBG {main} {_quote(reel_items[i][0])} AUTO", reel_items[i - 1][1])
            for i in range(1, len(reel_items))
        ]
        # CM IN (生→CM) の手動操作では reel 末尾に生復帰ステップを積む (docs/operations.md O5)。
        # 最後の CM の尺だけ待って生を AUTO ロード → CM 終了で CasparCG が自動的に本線を生へ戻す。
        step = _return_rtmp_step(main, params, reel_items[-1][1])
        if step:
            reel_steps.append(step)
        return AmcpPlan(
            loadbg=loadbg,
            take=f"PLAY {main}",
            is_immediate=False,
            reel=tuple(reel_steps),
        )

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_VT:
        # 生番組内 VT (録画セグメント) ロール (#25 §6.4)。CM とは別建て (billing/CG バンパー
        # 対象外) — cg=... は設定しない (VT にバンパーは出さない、ユーザー確定判断)。
        pv = event.play_vt
        clip, in_ms, out_ms = pv.clip, pv.in_ms, pv.out_ms
        seek = ms_to_frames(in_ms, fps)
        length = ms_to_frames(max(0, out_ms - in_ms), fps)
        loadbg = f"LOADBG {main} {_quote(clip)} SEEK {seek} LENGTH {length} MIX 15"
        reel_steps: list[tuple[str, int]] = []
        step = _return_rtmp_step(main, params, max(0, out_ms - in_ms))
        if step:
            reel_steps.append(step)
        return AmcpPlan(
            loadbg=loadbg,
            take=f"PLAY {main}",
            is_immediate=False,
            reel=tuple(reel_steps),
        )

    if action == playout_pb2.PLAYOUT_ACTION_YT_TRANSITION:
        # YouTube は agent 対象外 (server youtube/tasks.py が Data API で transition)。
        return AmcpPlan(loadbg=None, take="", is_immediate=True)

    if action == playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE:
        # docs/operations.md O4: スレート解除。slate layer N-90 を空にする (本線 N-10 は保持済)。
        return AmcpPlan(loadbg=None, take=f"CLEAR {channel}-{LAYER_SLATE}", is_immediate=True)

    if action == playout_pb2.PLAYOUT_ACTION_OVERLAY_OP:
        # 手動グラフィック (#18 §B): 本線に触れず対象レイヤへ 1 コマンド即時発射。
        return AmcpPlan(loadbg=None, take=overlay_op_cmd(channel, params), is_immediate=True)

    raise ValueError(f"unknown action: {action}")


def retake_plan(
    event: playout_pb2.PlayoutEvent,
    *,
    channel: int,
) -> tuple[str, ...]:
    """casparcg 再起動後に「現行イベント」を本線 layer へ貼り直す即応コマンド列 (#7)。

    casparcg を再起動すると LOADBG 済み背面も前面 PLAY も失われる。executed 済みの現行
    イベントは due_for_take に再び乗らないため、再接続を検知したらこの列で本線を即時に
    貼り直す (手動復帰 PLAY {ch}-10 "<clip>" LOOP と同じ即応経路。LOADBG を介さない)。

    play_asset / play_filler → PLAY ... LOOP (有限尺が再起動までに本来終わっていても黒落ち
      より継続を優先。次の予定 TAKE で自然に置換される)。
    cut_live → 生は LOOP/SEEK 不可。LOADBG ... MIX → PLAY で再 ingest。
    それ以外 (CM/bundle/slate 等) は本線の持続状態ではないので空 () を返す
      (再 take でループさせると広告が無限再生になる等のため)。
    """
    action = event.action
    params = dict(event.params)
    main = f"{channel}-{LAYER_MAIN}"

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_ASSET:
        clip = event.play_asset.clip or params.get("clip") or f"asset/{params.get('asset_id', '?')}"
        return (f"PLAY {main} {_quote(clip)} LOOP",)

    if action == playout_pb2.PLAYOUT_ACTION_PLAY_FILLER:
        clip = (
            event.play_filler.clip
            or params.get("clip")
            or f"filler/{params.get('filler_playlist_id', '?')}"
        )
        return (f"PLAY {main} {_quote(clip)} LOOP",)

    if action == playout_pb2.PLAYOUT_ACTION_CUT_LIVE:
        rtmp_url = event.cut_live.rtmp_url or _rtmp_url_from(params)
        return (f"LOADBG {main} {_quote(rtmp_url)} MIX 15", f"PLAY {main}")

    return ()


def slate_command(channel: int, slate_clip: str) -> str:
    """goto_slate(channel) 用 (docs/casparcg.md §5.4)。clip 付き PLAY で即応退避。"""
    return f"PLAY {channel}-{LAYER_SLATE} {_quote(slate_clip)} LOOP"


def clear_slate_command(channel: int) -> str:
    """スレート解除 (docs/operations.md O4)。slate layer N-90 を空にする。本線 N-10 は保持。"""
    return f"CLEAR {channel}-{LAYER_SLATE}"


# exposure_policy (#27、docs/site-only-broadcast.md §4.4)。公開ミラー M / メンバーミラー P の
# 出力先レイヤ。本線と同じ layer 番号 (10) だが channel が別 (mirror_channel は送出ノード固有の
# 別 CasparCG channel 番号で、agent 側 env から MirrorController が受け取る)。
LAYER_MIRROR = 10


def yt_mirror_filler_command(mirror_channel: int, filler_clip: str) -> str:
    """YTミラー(公開M/メンバーP)を案内フィラーへ差し替え (slate_command と同流儀)。"""
    return f"PLAY {mirror_channel}-{LAYER_MIRROR} {_quote(filler_clip)} LOOP"


def yt_mirror_route_command(mirror_channel: int, main_channel: int) -> str:
    """YTミラーを本線(route://N。channel合成出力)へ復帰する。

    route://N-10 のように layer を指定すると本線の layer10 のみの合成になりサイトと画が
    食い違うため、必ず layer 無し (route://N) で channel 全体の合成出力を渡す。
    """
    return f"PLAY {mirror_channel}-{LAYER_MIRROR} route://{main_channel}"


def main_dip_cmds(
    channel: int, *, level: float, frames: int, tween: str = "linear"
) -> tuple[str, str]:
    """本線 (N-10) の OPACITY/VOLUME を frames かけて level へトゥイーンする MIXER コマンド対 (§1.3)。

    フィラー↔番組の境界を「黒フェード(dip to black)」で繋ぐ用途。level=0.0 で黒(下地)+無音へ、
    level=1.0 で通常へ戻す。frames は fps 連動 (30fps なら 15≒0.5s)。MIXER transform は
    producer 入替 (LOADBG/PLAY) で保持されるため、フェードアウト→テイク→フェードインの順で
    本線を継ぎ目に黒落とししつつ切替えられる (テイク自体は引数なし PLAY のハードカット)。
    """
    n = f"{channel}-{LAYER_MAIN}"
    return (
        f"MIXER {n} OPACITY {level} {frames} {tween}",
        f"MIXER {n} VOLUME {level} {frames} {tween}",
    )


def main_reset_levels_cmds(channel: int) -> tuple[str, str]:
    """本線 (N-10) の OPACITY/VOLUME を即時 1.0 へ戻す MIXER コマンド対 (トゥイーン無し)。

    黒フェード途中でテイクに失敗した場合などに、本線を黒+無音のまま残さないための即時復帰。
    """
    n = f"{channel}-{LAYER_MAIN}"
    return (f"MIXER {n} OPACITY 1.0", f"MIXER {n} VOLUME 1.0")
