# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""字幕(WebVTT)の自動生成 (#ADMIN-04 / PLAYER-04・決定#24)。

正規化済み mezzanine (R2) の音声を faster-whisper で文字起こしし、WebVTT を R2 に保存する。
呼び出しは medialib.tasks.transcribe_asset から (queue='captions'・専任 pod・concurrency 1)。
GPU 無し環境ゆえ int8 CPU 推論。normalize と競合させないため別キュー/別 pod で直列化する。

送出経路には一切触れない: 線形ライブへの字幕は encoder/manifest 改変になるため対象外で、
録画が VOD 化した素材 (kind=program) にだけ字幕を付ける (docs/viewing-experience.md §2)。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core import r2
from medialib.models import Asset, AssetKind, CaptionStatus, NormalizeStatus

logger = logging.getLogger(__name__)

# 音声抽出 ffmpeg の無限ハング打ち切り (本物のハング救済)。抽出は 16kHz mono へ落とすだけなので
# 実時間の数分の一で済むが、長尺 + ネットワーク入力を見込み余裕を持たせる。
# invariant: _EXTRACT_TIMEOUT ≤ Celery soft_time_limit ≤ hard time_limit < visibility_timeout。
_EXTRACT_TIMEOUT = int(os.environ.get("CAPTION_EXTRACT_TIMEOUT", "3600"))  # 1h
# mezzanine 署名 URL の有効期限 (抽出中に切れない長さ)。
_SRC_URL_EXPIRES = int(os.environ.get("CAPTION_SRC_URL_EXPIRES", "7200"))  # 2h

_model: Any = None  # faster-whisper モデルの singleton (ロードが重いので pod 内で使い回す)


def _get_model() -> Any:
    """WhisperModel を遅延ロードして使い回す。faster-whisper は captions pod でのみ import する。"""
    global _model
    if _model is None:
        # 遅延 import: web/normalize/worker pod では import しない (ctranslate2 のロードは重い)。
        from faster_whisper import WhisperModel

        name = os.environ.get("WHISPER_MODEL", "small")
        device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        logger.info(
            "faster-whisper ロード model=%s device=%s compute=%s", name, device, compute_type
        )
        _model = WhisperModel(name, device=device, compute_type=compute_type)
    return _model


def _fmt_ts(seconds: float) -> str:
    """WebVTT タイムスタンプ HH:MM:SS.mmm。"""
    if seconds < 0:
        seconds = 0.0
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _extract_audio(src_url: str, wav_path: Path) -> None:
    """mezzanine から 16kHz mono WAV を 1 パスで抽出 (映像デコード回避で桁違いに安い)。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=_EXTRACT_TIMEOUT)


def build_vtt(segments) -> str:
    """faster-whisper のセグメント列 (start/end/text) を WebVTT 文字列に整形。

    音声が無い/発話が無い場合も有効な空 VTT (ヘッダのみ) を返す (READY 化して再処理を防ぐ)。
    """
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines) + "\n"


def caption_key(asset: Asset, lang: str) -> str:
    return f"captions/{asset.kind}/{asset.id}.{lang}.vtt"


def transcribe(asset: Asset) -> None:
    """asset の mezzanine 音声を文字起こしし WebVTT を R2 に保存 → caption_* を更新。

    前提: 正規化 READY + r2_key あり + kind=program。失敗時は caption_status=failed で例外再送出。
    """
    if asset.kind != AssetKind.PROGRAM:
        logger.info("字幕生成スキップ asset=%s kind=%s (番組素材のみ対象)", asset.id, asset.kind)
        return
    if asset.caption_status == CaptionStatus.READY and asset.caption_r2_key:
        # 既に使える字幕がある素材は上書きしない (最後の砦・決定#26)。slidecast 取り込みが
        # 台本由来の VTT を先付けした素材は _maybe_enqueue_captions のガードで通常ここへ来ないが、
        # ローリングデプロイの混在バージョン窓 (旧 normalize pod がガード無しで enqueue する) で
        # whisper が正確な先付け字幕を潰す事故が dev で実発生した (2026-07-09)。
        logger.info("字幕生成スキップ asset=%s (READY の字幕が既にあるため上書きしない)", asset.id)
        return
    if asset.normalize_status != NormalizeStatus.READY or not asset.r2_key:
        raise ValueError(f"asset {asset.id} は未正規化 (字幕生成不可)")

    lang = asset.caption_lang or os.environ.get("WHISPER_LANG", "ja")
    asset.caption_status = CaptionStatus.PROCESSING
    asset.caption_error = None
    asset.save(update_fields=["caption_status", "caption_error"])

    wav_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        src_url = r2.presign_get(asset.r2_key, expires=_SRC_URL_EXPIRES)
        _extract_audio(src_url, wav_path)

        model = _get_model()
        # vad_filter で無音を刈り、日本語を明示。segments はジェネレータ (反復時に推論が走る)。
        segments, _info = model.transcribe(str(wav_path), language=lang, vad_filter=True)
        vtt = build_vtt(segments)

        key = caption_key(asset, lang)
        r2.put_object(key, vtt.encode("utf-8"), "text/vtt; charset=utf-8")

        asset.caption_status = CaptionStatus.READY
        asset.caption_r2_key = key
        asset.caption_lang = lang
        asset.caption_error = None
        asset.save(
            update_fields=["caption_status", "caption_r2_key", "caption_lang", "caption_error"]
        )
        logger.info("字幕生成 done asset=%s key=%s lang=%s", asset.id, key, lang)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")[-2000:]
        asset.caption_status = CaptionStatus.FAILED
        asset.caption_error = f"ffmpeg(音声抽出) failed: {stderr}"
        asset.save(update_fields=["caption_status", "caption_error"])
        raise
    except Exception as e:
        asset.caption_status = CaptionStatus.FAILED
        asset.caption_error = str(e)[:2000]
        asset.save(update_fields=["caption_status", "caption_error"])
        raise
    finally:
        if wav_path is not None and wav_path.exists():
            with contextlib.suppress(OSError):
                wav_path.unlink()
