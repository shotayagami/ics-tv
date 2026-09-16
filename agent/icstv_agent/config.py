# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""agent 設定 (環境変数駆動。12factor)。"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    server_target: str  # 例: server.icstv.local:50051 (WireGuard 越しの gRPC)
    auth_token: str  # gRPC metadata authorization: Bearer <token>
    channel_slug: str
    queue_db_path: Path  # SQLite ローカルキュー
    caspar_host: str  # CasparCG AMCP
    caspar_port: int
    caspar_channel: int  # AMCP channel 番号 (本線 N-10 の N)
    fps: int  # CasparCG channel の出力 fps (SEEK/LENGTH 換算)
    slate_clip: str  # goto_slate(channel) が再生する clip 名
    preroll_sec: int  # LOADBG を PLAY の何秒前に発火するか
    # フィラー↔番組の境界を黒フェード(dip to black)で繋ぐ片側のフェード長(ms)。
    # フェードアウト(filler→黒)・フェードイン(番組→通常)とも同じ長さ。0 でハードカット(従来挙動)。
    transition_ms: int
    heartbeat_interval_sec: int
    # feed monitor (生入力 publisher 監視。docs/operations.md O2/O6)
    mediamtx_api_url: str  # MediaMTX HTTP API (publisher 状態)
    feed_poll_sec: float  # 監視 tick 周期
    feed_lost_ticks: int  # publisher 消失を何 tick 連続で「断」と判定するか
    feed_hysteresis_ticks: int  # 復帰を何 tick 連続で確認してから自動復帰するか
    feed_flap_window_sec: int  # フラップ判定の窓
    feed_flap_max: int  # 窓内の自動復帰がこれを超えたらサスペンド
    # prefetch (R2 mezzanine → CasparCG ローカル媒体フォルダ先読み。overview 3.2)
    media_dir: Path  # CasparCG の媒体フォルダ (clip 名はここからの相対で解決)
    cache_max_bytes: int  # LRU 退避の上限
    prefetch_ahead_sec: int  # 何秒先までの event を先読みするか
    prefetch_poll_sec: float  # prefetch ループの周期
    prefetch_limit: int  # 1 周あたりの最大先読み件数
    # 直近この秒数に受信した event (実行済み含む) の clip も先読みする。filler は just-in-time
    # 発行で受信即 executed され未来候補から漏れるため、反復する次回に間に合わせる (自己修復)。
    prefetch_recent_sec: int
    # 出力 watchdog (本線が黒/フリーズ=「AMCP健全だが出力不健全」の盲点を検知し自動 re-take)。
    watchdog_poll_sec: float  # 本線 foreground をこの周期で監視
    watchdog_bad_ticks: int  # 連続でこの回数 bad なら re-take (ヒステリシス)
    # 生放送録画 (record_live)。MediaMTX に録画させるローカルディレクトリと、
    # 送出ノードのディスク逼迫時に録画を諦める閾値 (本線送出には影響させない best-effort ガード)。
    recording_dir: Path
    recording_min_free_bytes: int
    recording_upload_poll_sec: float  # アップロード再送 (outbox) の周期
    # exposure_policy (#27、docs/site-only-broadcast.md §4.4)。ミラー channel 未設定 (None) の
    # ノードは MirrorController が全メソッド no-op になり既存 1ch/2ch ノードを壊さない。
    yt_mirror_caspar_channel: int | None  # 公開ミラー M の AMCP channel 番号
    yt_members_caspar_channel: int | None  # メンバーミラー P の AMCP channel 番号
    site_only_filler_clip: str  # 公開ミラー既定フィラー (manifest 未着時のノードローカル fallback)
    members_filler_clip: str  # メンバーミラー既定フィラー (同上)


def load() -> Config:
    return Config(
        server_target=os.environ["ICSTV_SERVER_GRPC"],
        auth_token=os.environ["ICSTV_AGENT_TOKEN"],
        channel_slug=os.environ["ICSTV_CHANNEL_SLUG"],
        queue_db_path=Path(
            os.environ.get("ICSTV_QUEUE_DB", "~/.cache/icstv-agent/queue.db"),
        ).expanduser(),
        caspar_host=os.environ.get("ICSTV_CASPAR_HOST", "127.0.0.1"),
        caspar_port=int(os.environ.get("ICSTV_CASPAR_PORT", "5250")),
        caspar_channel=int(os.environ.get("ICSTV_CASPAR_CHANNEL", "1")),
        fps=int(os.environ.get("ICSTV_FPS", "60")),
        slate_clip=os.environ.get("ICSTV_SLATE_CLIP", "slate/please_wait"),
        preroll_sec=int(os.environ.get("ICSTV_PREROLL_SEC", "5")),
        transition_ms=int(os.environ.get("ICSTV_TRANSITION_MS", "500")),
        heartbeat_interval_sec=int(os.environ.get("ICSTV_HEARTBEAT_SEC", "30")),
        mediamtx_api_url=os.environ.get("ICSTV_MEDIAMTX_API", "http://127.0.0.1:9997"),
        feed_poll_sec=float(os.environ.get("ICSTV_FEED_POLL_SEC", "1.0")),
        feed_lost_ticks=int(os.environ.get("ICSTV_FEED_LOST_TICKS", "2")),
        feed_hysteresis_ticks=int(os.environ.get("ICSTV_FEED_HYSTERESIS_TICKS", "10")),
        feed_flap_window_sec=int(os.environ.get("ICSTV_FEED_FLAP_WINDOW_SEC", "600")),
        feed_flap_max=int(os.environ.get("ICSTV_FEED_FLAP_MAX", "3")),
        media_dir=Path(
            os.environ.get("ICSTV_MEDIA_DIR", "~/.cache/icstv-agent/media"),
        ).expanduser(),
        cache_max_bytes=int(os.environ.get("ICSTV_CACHE_MAX_BYTES", str(50 * 1024**3))),
        prefetch_ahead_sec=int(os.environ.get("ICSTV_PREFETCH_AHEAD_SEC", "14400")),  # 4h
        prefetch_poll_sec=float(os.environ.get("ICSTV_PREFETCH_POLL_SEC", "60")),
        prefetch_limit=int(os.environ.get("ICSTV_PREFETCH_LIMIT", "128")),
        prefetch_recent_sec=int(os.environ.get("ICSTV_PREFETCH_RECENT_SEC", "900")),  # 15min
        watchdog_poll_sec=float(os.environ.get("ICSTV_WATCHDOG_POLL_SEC", "3")),
        watchdog_bad_ticks=int(os.environ.get("ICSTV_WATCHDOG_BAD_TICKS", "3")),
        recording_dir=Path(
            os.environ.get("ICSTV_RECORDING_DIR", "/var/lib/icstv-agent/recordings"),
        ).expanduser(),
        # 5 GiB を下回ったら録画をスキップ (ディスク逼迫で本線送出を巻き込まないための簡易ガード)。
        recording_min_free_bytes=int(
            os.environ.get("ICSTV_RECORDING_MIN_FREE_BYTES", str(5 * 1024**3))
        ),
        recording_upload_poll_sec=float(os.environ.get("ICSTV_RECORDING_UPLOAD_POLL_SEC", "60")),
        yt_mirror_caspar_channel=(
            int(v) if (v := os.environ.get("ICSTV_YT_MIRROR_CASPAR_CHANNEL")) else None
        ),
        yt_members_caspar_channel=(
            int(v) if (v := os.environ.get("ICSTV_YT_MEMBERS_CASPAR_CHANNEL")) else None
        ),
        site_only_filler_clip=os.environ.get(
            "ICSTV_SITE_ONLY_FILLER_CLIP", "filler/site_only_default"
        ),
        members_filler_clip=os.environ.get("ICSTV_MEMBERS_FILLER_CLIP", "filler/members_default"),
    )
