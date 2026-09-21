# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""mezzanine エンコード仕様と ffmpeg/ffprobe コマンド構築 (純粋モジュール)。

Django/DB/R2 に依存しない。クラスタ内の正規化 (medialib.normalize) と Windows オフロード
watcher (tools/normalize_watcher.py — Django を積まない軽量イメージ) の両方が import する。
エンコードロジックをここに一元化し、クラスタと Windows の間で仕様ドリフトが構造的に
起きないようにする (docs/normalize-offload.md §2/§5)。

spec は request.json にそのまま焼き込める JSON 互換 dict。値の意味:
- width/height/fps: mezzanine 規格 (既定 1920x1080/60)
- vcodec/preset/profile/pix_fmt/vbitrate: 映像エンコード
- acodec/abitrate/arate: 音声エンコード
- loudnorm_i/lra/tp: ラウドネス目標 (docs/delivery.md D5: -14 LUFS / LRA 11 / TP -1.5 dBTP)
- container: 出力コンテナ (mp4)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ffmpeg/ffprobe の無限ハング打ち切り (本物のハング救済)。encode は番組長尺だと CPU で数時間
# かかりうるため既定は大きめ。invariant: FFMPEG_TIMEOUT ≤ Celery soft ≤ hard < visibility_timeout。
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "21600"))  # 6h
PROBE_TIMEOUT = int(os.environ.get("FFPROBE_TIMEOUT", "300"))  # 5min (解析は短い)

# spec ホワイトリスト (watcher が R2 由来の spec を検証するときの許容値)。
# ffmpeg 引数は execv 形式 (シェルを経由しない) なので任意コマンド実行は元々不可能だが、
# 汚染された request.json で不正なフィルタ/コーデックを注入されないよう型と列挙で縛る。
_ALLOWED_VCODECS = {"libx264", "h264_nvenc", "h264_qsv", "h264_vaapi"}
_ALLOWED_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    # NVENC 系 preset (将来の HW encode 用に許容)
    "p1",
    "p2",
    "p3",
    "p4",
    "p5",
    "p6",
    "p7",
}
_ALLOWED_ACODECS = {"aac"}
_ALLOWED_PIX_FMTS = {"yuv420p"}
_ALLOWED_PROFILES = {"baseline", "main", "high"}
_ALLOWED_CONTAINERS = {"mp4"}
_BITRATE_RE = re.compile(r"^\d{1,6}[kKmM]?$")
_NUM_RE = re.compile(r"^-?\d{1,4}(\.\d{1,4})?$")


def spec_from_env() -> dict:
    """環境変数 (MEZZ_* / LOUDNORM_*) から mezzanine spec を構築。クラスタ側の唯一の真実源。"""
    return {
        "width": int(os.environ.get("MEZZ_WIDTH", "1920")),
        "height": int(os.environ.get("MEZZ_HEIGHT", "1080")),
        "fps": int(os.environ.get("MEZZ_FPS", "60")),
        "vbitrate": os.environ.get("MEZZ_VBITRATE", "16M"),
        "abitrate": os.environ.get("MEZZ_ABITRATE", "192k"),
        "arate": os.environ.get("MEZZ_ARATE", "48000"),
        # vcodec/preset は env で差し替え可。HW encode (h264_nvenc/h264_qsv/h264_vaapi) や
        # 高速 preset (faster/veryfast) に切替えてエンコードを短縮する余地を残す。
        # 既定は従来どおり libx264 + medium (master 品質を変えない安全側)。
        "vcodec": os.environ.get("MEZZ_VCODEC", "libx264"),
        "preset": os.environ.get("MEZZ_PRESET", "medium"),
        "acodec": "aac",
        "pix_fmt": "yuv420p",
        "profile": "high",
        "container": "mp4",
        "loudnorm_i": os.environ.get("LOUDNORM_I", "-14"),
        "loudnorm_lra": os.environ.get("LOUDNORM_LRA", "11"),
        "loudnorm_tp": os.environ.get("LOUDNORM_TP", "-1.5"),
    }


def validate_spec(raw: dict) -> dict:
    """信頼できない入力 (R2 経由の request.json) の spec を検証して正規形にする。

    watcher 側の多層防御。許容外の値は ValueError (ジョブを failed にする)。
    """
    if not isinstance(raw, dict):
        raise ValueError("spec is not a dict")
    out: dict = {}
    for key, lo, hi in (("width", 16, 7680), ("height", 16, 4320), ("fps", 1, 240)):
        v = raw.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"spec.{key} invalid: {v!r}")
        out[key] = v
    for key, allowed in (
        ("vcodec", _ALLOWED_VCODECS),
        ("preset", _ALLOWED_PRESETS),
        ("acodec", _ALLOWED_ACODECS),
        ("pix_fmt", _ALLOWED_PIX_FMTS),
        ("profile", _ALLOWED_PROFILES),
        ("container", _ALLOWED_CONTAINERS),
    ):
        v = raw.get(key)
        if v not in allowed:
            raise ValueError(f"spec.{key} invalid: {v!r}")
        out[key] = v
    for key in ("vbitrate", "abitrate"):
        v = raw.get(key)
        if not isinstance(v, str) or not _BITRATE_RE.match(v):
            raise ValueError(f"spec.{key} invalid: {v!r}")
        out[key] = v
    v = raw.get("arate")
    if not isinstance(v, str) or not v.isdigit() or not 8000 <= int(v) <= 192000:
        raise ValueError(f"spec.arate invalid: {v!r}")
    out["arate"] = v
    for key in ("loudnorm_i", "loudnorm_lra", "loudnorm_tp"):
        v = raw.get(key)
        if not isinstance(v, str) or not _NUM_RE.match(v):
            raise ValueError(f"spec.{key} invalid: {v!r}")
        out[key] = v
    return out


def loudnorm_first_pass(
    src: Path | str, spec: dict, *, timeout: int = FFMPEG_TIMEOUT
) -> dict | None:
    """loudnorm 1-pass で measured 値を取得 (docs/delivery.md D4)。

    print_format=json が stderr 末尾に出す JSON を拾う。失敗/音声無しなら None (loudnorm 省略)。
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(src),
        "-af",
        f"loudnorm=I={spec['loudnorm_i']}:LRA={spec['loudnorm_lra']}"
        f":TP={spec['loudnorm_tp']}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("loudnorm 1-pass timeout — loudnorm 省略")
        return None
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def loudnorm_filter(meas: dict, spec: dict) -> str:
    """1-pass の measured 値を埋めた 2-pass loudnorm フィルタ文字列 (linear=true)。"""
    return (
        f"loudnorm=I={spec['loudnorm_i']}:LRA={spec['loudnorm_lra']}:TP={spec['loudnorm_tp']}"
        f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
        f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
        f":offset={meas['target_offset']}:linear=true"
    )


def build_normalize_cmd(
    src: Path | str, dst: Path | str, spec: dict, meas: dict | None
) -> list[str]:
    """通常正規化 (解像度/fps 統一 + 2-pass loudnorm) の ffmpeg 引数を構築。"""
    vf = (
        f"scale={spec['width']}:{spec['height']}:force_original_aspect_ratio=decrease,"
        f"pad={spec['width']}:{spec['height']}:(ow-iw)/2:(oh-ih)/2"
    )
    audio_opts = (["-af", loudnorm_filter(meas, spec)] if meas else []) + ["-ar", spec["arate"]]
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c:v",
        spec["vcodec"],
        "-preset",
        spec["preset"],
        "-profile:v",
        spec["profile"],
        "-pix_fmt",
        spec["pix_fmt"],
        "-vf",
        vf,
        "-r",
        str(spec["fps"]),
        "-b:v",
        spec["vbitrate"],
        *audio_opts,
        "-c:a",
        spec["acodec"],
        "-b:a",
        spec["abitrate"],
        "-movflags",
        "+faststart",
        str(dst),
    ]


def build_passthrough_cmd(
    src: Path | str, dst: Path | str, spec: dict, meas: dict | None
) -> list[str]:
    """長尺 passthrough (映像コピー + 音声 loudnorm) の ffmpeg 引数を構築。"""
    if meas:
        audio = [
            "-af",
            loudnorm_filter(meas, spec),
            "-ar",
            spec["arate"],
            "-c:a",
            spec["acodec"],
            "-b:a",
            spec["abitrate"],
        ]
    else:
        audio = ["-c:a", "copy"]
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c:v",
        "copy",
        *audio,
        "-movflags",
        "+faststart",
        str(dst),
    ]


def ffprobe(path: Path | str, *, timeout: int = PROBE_TIMEOUT) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    return json.loads(subprocess.check_output(cmd, timeout=timeout))


def probe_duration_sec(path: Path | str, *, timeout: int = PROBE_TIMEOUT) -> float | None:
    """ソース尺(秒)を ffprobe で取得。取得不可なら None (呼び出し側が安全側へ倒す)。"""
    try:
        return float(ffprobe(path, timeout=timeout)["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError, OSError):
        return None


def stream(probe: dict, kind: str) -> dict | None:
    return next((s for s in probe["streams"] if s["codec_type"] == kind), None)


def fps_from_str(s: str | None) -> float | None:
    if not s or "/" not in s:
        return None
    num, den = s.split("/")
    if not int(den):
        return None
    return round(int(num) / int(den), 3)
