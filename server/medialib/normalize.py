# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ffmpeg 正規化 + R2 (S3 互換) アップロード (docs/overview.md 3.2)。

「正規化しないと継ぎ目で映像が乱れる」ため、解像度/fps/コーデック/音声を mezzanine 仕様に
強制統一する。Phase 1 の既定は 1080p60 H.264 + AAC 192k (env で override 可)。

呼び出しは medialib.tasks.normalize_asset から (queue='normalize')。

エンコード仕様/ffmpeg コマンド構築/ffprobe は medialib.mezz に一元化し、クラスタ内正規化と
Windows オフロード watcher で共有する (docs/normalize-offload.md §5)。本モジュールは R2 取得/
アップロードと Asset DB 更新のオーケストレーションを担う。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from django.utils import timezone

from core import r2
from medialib import mezz
from medialib.models import Asset, NormalizeStatus

logger = logging.getLogger(__name__)

# 長尺ガード: この CPU 専用環境では極端な長尺が soft_time_limit(6h) に収まらず
# SoftTimeLimitExceeded で failed になり、しかも 1 本が worker 枠と CPU を 6h 占有する。
# encode 前に尺を測り、上限超は encode せず即 failed にして無駄な 6h を防ぐ (再投入は運用者の
# 「再正規化」に委ねる)。0 = 無効。HW(h264_nvenc 等)導入後は引き上げ/無効化してよい。
_MAX_SOURCE_SEC = int(os.environ.get("MEZZ_MAX_SOURCE_SEC", "0"))


def _resolve_source(source_path: str) -> tuple[Path, bool]:
    """source_path を実ファイルに解決。`r2://<key>` は R2 quarantine から temp へ DL する。

    納品ポータルの承認は asset.source_path に `r2://<quarantine_key>` を入れるため、
    ローカルパス (admin 投入) と R2 原本 (オンライン納品) の両方を扱えるようにする。
    戻り値 (path, is_temp)。is_temp=True の一時ファイルは呼び出し側が後始末する。
    """
    if source_path.startswith("r2://"):
        key = source_path[len("r2://") :]
        fd, name = tempfile.mkstemp(suffix=".src")
        os.close(fd)
        tmp = Path(name)
        r2.client().download_file(r2.bucket(), key, str(tmp))
        return tmp, True
    p = Path(source_path)
    if not p.exists():
        raise FileNotFoundError(f"source not found: {p}")
    return p, False


def apply_ready_metadata(asset: Asset, *, r2_key: str, probe: dict, passthrough: bool) -> int:
    """ffprobe 結果を Asset の READY メタに反映して保存し duration_ms を返す。

    正規化 (ローカル) と オフロード finalize (reconciler) の両方から使う共通処理。
    update_fields で正規化系フィールドのみ保存し、並行して更新されうる caption_status 等を
    巻き戻さない (docs/normalize-offload.md §3.4)。
    """
    v = mezz.stream(probe, "video") or {}
    a = mezz.stream(probe, "audio") or {}
    duration_ms = int(float(probe["format"]["duration"]) * 1000)
    asset.r2_key = r2_key
    asset.duration_ms = duration_ms
    asset.width = v.get("width")
    asset.height = v.get("height")
    asset.fps = mezz.fps_from_str(v.get("avg_frame_rate"))
    asset.vcodec = v.get("codec_name")
    asset.acodec = a.get("codec_name")
    asset.normalize_status = NormalizeStatus.READY
    asset.passthrough = passthrough
    asset.normalize_error = None
    asset.save(
        update_fields=[
            "r2_key",
            "duration_ms",
            "width",
            "height",
            "fps",
            "vcodec",
            "acodec",
            "normalize_status",
            "passthrough",
            "normalize_error",
        ]
    )
    return duration_ms


def _finalize_output(asset: Asset, tmp_path: Path, spec: dict, *, passthrough: bool) -> int:
    """正規化 / passthrough 共通の仕上げ: 出力を ffprobe → R2 upload → asset メタ更新 (READY)。

    duration_ms を返す。passthrough フラグも必ず上書きする (再正規化で原本→正規化に戻したときに
    フラグが残らないよう、正常路は passthrough=False を明示する)。
    """
    probe = mezz.ffprobe(tmp_path)
    r2_key = f"mezzanine/{asset.kind}/{asset.id}.{spec['container']}"
    bucket = r2.bucket()
    with tmp_path.open("rb") as fp:
        r2.client().upload_fileobj(
            fp,
            bucket,
            r2_key,
            ExtraArgs={"ContentType": f"video/{spec['container']}"},
        )
    return apply_ready_metadata(asset, r2_key=r2_key, probe=probe, passthrough=passthrough)


def _passthrough(asset: Asset, src: Path, spec: dict, src_sec: float) -> None:
    """長尺で映像の再エンコードを諦めた素材を「映像コピー + 音声 loudnorm」で素材化 (READY)。

    重い映像 encode を回避しつつ送出可能にする。映像は原本の解像度/fps のまま (playout=CasparCG が
    実行時に変換)。音声のみ loudnorm で -14 LUFS に揃え、継ぎ目の音量段差を防ぐ (音声無し/測定不可は
    音声もコピー)。passthrough=True で UI に原本バッジ。
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{spec['container']}", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        meas = mezz.loudnorm_first_pass(src, spec)
        if not meas:
            logger.warning("passthrough loudnorm 測定不可 asset=%s — 音声もコピー", asset.id)
        cmd = mezz.build_passthrough_cmd(src, tmp_path, spec, meas)
        logger.info("ffmpeg passthrough asset=%s cmd=%s", asset.id, shlex.join(cmd))
        subprocess.run(cmd, check=True, capture_output=True, timeout=mezz.FFMPEG_TIMEOUT)
        duration_ms = _finalize_output(asset, tmp_path, spec, passthrough=True)
        logger.warning(
            "normalize passthrough asset=%s dur=%.0fs > %ds → 映像コピーで素材化 duration_ms=%d",
            asset.id,
            src_sec,
            _MAX_SOURCE_SEC,
            duration_ms,
        )
    finally:
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def normalize(asset: Asset) -> None:
    """source_path を mezzanine に正規化 → R2 にアップロード → asset を更新。

    失敗時は normalize_status=failed + normalize_error をセットして例外再送出。
    Celery 側で retry 制御。
    """
    if not asset.source_path:
        raise ValueError(f"asset {asset.id} has no source_path")

    spec = mezz.spec_from_env()
    asset.normalize_status = NormalizeStatus.PROCESSING
    asset.normalize_error = None
    asset.normalize_started_at = timezone.now()
    asset.save(update_fields=["normalize_status", "normalize_error", "normalize_started_at"])

    tmp_path: Path | None = None
    src_tmp: Path | None = None  # r2:// ソースを DL した場合の一時原本
    try:
        # 納品ポータル承認は source_path="r2://<quarantine_key>" を渡す。R2 から取得して扱う。
        src, src_is_tmp = _resolve_source(asset.source_path)
        if src_is_tmp:
            src_tmp = src

        # 長尺ガード (MEZZ_MAX_SOURCE_SEC>0 で有効)。上限超は重い映像 encode を諦め、
        # passthrough (映像コピー + 音声loudnorm) で素材化して送出可 (READY) にする。
        # 例外を出さず return し、Celery 再試行を起こさない (決定論的なので poison loop になる)。
        if _MAX_SOURCE_SEC > 0:
            src_sec = mezz.probe_duration_sec(src)
            if src_sec is not None and src_sec > _MAX_SOURCE_SEC:
                _passthrough(asset, src, spec, src_sec)
                return

        with tempfile.NamedTemporaryFile(suffix=f".{spec['container']}", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        # loudnorm 2-pass: 1-pass で measured 取得 → 2-pass で linear ゲイン適用 (D4/D5)。
        # 音声無し/測定失敗時は loudnorm 省略 (映像正規化は継続)。
        meas = mezz.loudnorm_first_pass(src, spec)
        if meas is None:
            logger.warning("loudnorm measured 取得不可 asset=%s — loudnorm 省略", asset.id)
        cmd = mezz.build_normalize_cmd(src, tmp_path, spec, meas)
        logger.info("ffmpeg normalize asset=%s cmd=%s", asset.id, shlex.join(cmd))
        subprocess.run(cmd, check=True, capture_output=True, timeout=mezz.FFMPEG_TIMEOUT)

        duration_ms = _finalize_output(asset, tmp_path, spec, passthrough=False)
        logger.info(
            "normalize done asset=%s r2=%s duration_ms=%d",
            asset.id,
            asset.r2_key,
            duration_ms,
        )

    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")[-2000:]
        asset.normalize_status = NormalizeStatus.FAILED
        asset.normalize_error = f"ffmpeg failed: {stderr}"
        asset.save(update_fields=["normalize_status", "normalize_error"])
        raise
    except Exception as e:
        asset.normalize_status = NormalizeStatus.FAILED
        asset.normalize_error = str(e)[:2000]
        asset.save(update_fields=["normalize_status", "normalize_error"])
        raise
    finally:
        for p in (tmp_path, src_tmp):
            if p is not None and p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
