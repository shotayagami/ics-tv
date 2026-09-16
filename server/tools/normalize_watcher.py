#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""正規化オフロード watcher (Windows/Docker 常駐・Django 非依存)。

R2 の normalize/in/<id>/request.json をポーリングし、原本を取得して mezzanine 仕様に
フルエンコード (medialib.mezz と同一ロジック) → normalize/out/<id>/ へ成果物を戻す。DB/Celery
broker には一切繋がない (連携面は R2 のみ)。クラスタ側の取り込み/フォールバックは
medialib.tasks.reconcile_normalize_offload が担う。設計正本: docs/normalize-offload.md。

イメージには本ファイルと medialib/mezz.py だけを載せる (Django 本体は不要)。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# mezz は Docker flat 配置 (/app/mezz.py) か repo 配置 (server/medialib/mezz.py) から解決する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "medialib"))
import mezz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("normalize-watcher")

HEARTBEAT_KEY = "normalize/watch/heartbeat.json"
IN_PREFIX = "normalize/in/"
WATCH_VERSION = "1"


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"env {name} required")
    return v


def make_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=_env("R2_ENDPOINT_URL"),
        aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("R2_REGION", "auto"),
    )


def _bucket() -> str:
    return _env("R2_BUCKET", "icstv-mezzanine")


def _interval() -> int:
    return int(os.environ.get("NORMALIZE_WATCH_INTERVAL_SEC", "20"))


def _progress_bump_sec() -> int:
    # encode 中に status.json を再 PUT して LastModified を更新する間隔 (クラスタの進捗停滞検出用)。
    return int(os.environ.get("NORMALIZE_WATCH_PROGRESS_BUMP_SEC", "120"))


# --- R2 小道具 (boto3 直叩き) ---


def put_json(client, bucket: str, key: str, obj: dict) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj).encode("utf-8"),
        ContentType="application/json",
    )


def get_json(client, bucket: str, key: str) -> dict | None:
    try:
        r = client.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    try:
        return json.loads(r["Body"].read())
    except (ValueError, TypeError):
        return None


def _out_prefix(asset_id) -> str:
    return f"normalize/out/{asset_id}/"


def status_key(asset_id) -> str:
    return f"{_out_prefix(asset_id)}status.json"


def result_key(asset_id) -> str:
    return f"{_out_prefix(asset_id)}result.json"


def mezz_out_key(asset_id) -> str:
    return f"{_out_prefix(asset_id)}mezz.mp4"


def heartbeat(client, bucket: str) -> None:
    put_json(
        client,
        bucket,
        HEARTBEAT_KEY,
        {
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": os.environ.get("HOSTNAME", "windows-render"),
            "version": WATCH_VERSION,
        },
    )


def list_requests(client, bucket: str) -> list[dict]:
    """in/<id>/request.json を列挙して中身を返す。"""
    out: list[dict] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": IN_PREFIX}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            if o["Key"].endswith("/request.json"):
                req = get_json(client, bucket, o["Key"])
                if req:
                    out.append(req)
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out


def needs_work(client, bucket: str, asset_id, request_id: str) -> bool:
    """このリクエストがまだ未処理か (claim 済/完了なら False)。

    slidecast の needsBuild と同型: result/status がこの requestId で既にあれば処理しない。
    building 中クラッシュの再実行はしない (クラスタが進捗停滞で fallback するのに委ねる)。
    """
    result = get_json(client, bucket, result_key(asset_id))
    if result and result.get("requestId") == request_id:
        return False
    # 既に claim 済 (encoding/failed) なら再処理しない。
    status = get_json(client, bucket, status_key(asset_id))
    return not (status and status.get("requestId") == request_id)


def _fail(client, bucket: str, asset_id, request_id: str, error: str) -> None:
    put_json(
        client,
        bucket,
        status_key(asset_id),
        {
            "assetId": asset_id,
            "requestId": request_id,
            "phase": "failed",
            "error": error[:2000],
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )
    logger.warning("asset=%s request=%s failed: %s", asset_id, request_id, error[:200])


def _run_ffmpeg_with_progress(
    client, bucket: str, asset_id, request_id: str, cmd: list[str]
) -> None:
    """ffmpeg をサブプロセス実行しつつ、encode 中に status.json を定期再 PUT (進捗ハートビート)。

    失敗時は CalledProcessError を投げる (呼び出し側が failed 化)。
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stop = threading.Event()

    def bump():
        while not stop.wait(_progress_bump_sec()):
            try:
                put_json(
                    client,
                    bucket,
                    status_key(asset_id),
                    {
                        "assetId": asset_id,
                        "requestId": request_id,
                        "phase": "encoding",
                        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                )
            except Exception as e:  # 進捗 PUT 失敗は encode を止めない
                logger.warning("progress bump 失敗 (無視): %s", e)

    t = threading.Thread(target=bump, daemon=True)
    t.start()
    try:
        _, stderr = proc.communicate(timeout=mezz.FFMPEG_TIMEOUT)
    finally:
        stop.set()
        t.join(timeout=5)
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-2000:]
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=tail.encode())


def process_one(client, bucket: str, req: dict) -> None:
    """1 リクエストを claim → 原本 DL → フルエンコード → 成果物 PUT → result.json。

    watcher は passthrough を行わない (CPU 潤沢なので長尺こそ本物の mezzanine を作る)。
    """
    asset_id = req.get("assetId")
    request_id = req.get("requestId")
    source_key = req.get("sourceKey")
    if asset_id is None or not request_id or not source_key:
        logger.warning("不正な request (必須欠落) を無視: %r", req)
        return

    # spec を検証 (R2 汚染に対する多層防御)。
    try:
        spec = mezz.validate_spec(req.get("spec") or {})
    except ValueError as e:
        _fail(client, bucket, asset_id, request_id, f"invalid spec: {e}")
        return

    # claim: status=encoding を先に PUT (早い者勝ちの状態フラグ)。
    put_json(
        client,
        bucket,
        status_key(asset_id),
        {
            "assetId": asset_id,
            "requestId": request_id,
            "phase": "encoding",
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )
    logger.info("asset=%s request=%s claim → encode", asset_id, request_id)

    workdir = Path(tempfile.mkdtemp(prefix=f"nrm-{asset_id}-"))
    src = workdir / "src"
    dst = workdir / f"mezz.{spec['container']}"
    try:
        client.download_file(bucket, source_key, str(src))
        meas = mezz.loudnorm_first_pass(src, spec)
        if meas is None:
            logger.warning("asset=%s loudnorm 測定不可 — loudnorm 省略", asset_id)
        cmd = mezz.build_normalize_cmd(src, dst, spec, meas)
        logger.info("asset=%s ffmpeg %s", asset_id, " ".join(cmd))
        _run_ffmpeg_with_progress(client, bucket, asset_id, request_id, cmd)

        # 出力を multipart upload (大容量対応は boto3 マネージド)。
        client.upload_file(
            str(dst),
            bucket,
            mezz_out_key(asset_id),
            ExtraArgs={"ContentType": f"video/{spec['container']}"},
        )
        # result.json を最後に PUT = 完了マーカー。sourceDurationSec を echo (クラスタの尺検証用)。
        put_json(
            client,
            bucket,
            result_key(asset_id),
            {
                "assetId": asset_id,
                "requestId": request_id,
                "sourceDurationSec": req.get("sourceDurationSec"),
                "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )
        logger.info("asset=%s request=%s done → review", asset_id, request_id)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace") if e.stderr else str(e)
        _fail(client, bucket, asset_id, request_id, f"ffmpeg failed: {stderr}")
    except Exception as e:  # 1 件の失敗で watcher を止めない
        _fail(client, bucket, asset_id, request_id, str(e))
    finally:
        _rmtree(workdir)


def _rmtree(path: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)


def poll_once(client, bucket: str) -> int:
    """1 周: heartbeat → 未処理 request を全部処理。処理件数を返す。"""
    heartbeat(client, bucket)
    handled = 0
    for req in list_requests(client, bucket):
        asset_id = req.get("assetId")
        request_id = req.get("requestId")
        if asset_id is None or not request_id:
            continue
        if not needs_work(client, bucket, asset_id, request_id):
            continue
        process_one(client, bucket, req)
        handled += 1
    return handled


def main() -> None:
    client = make_client()
    bucket = _bucket()
    interval = _interval()
    logger.info("normalize-watcher 起動 bucket=%s interval=%ds", bucket, interval)
    while True:
        try:
            n = poll_once(client, bucket)
            if n:
                logger.info("%d 件処理", n)
        except Exception as e:  # ポーリング自体の例外は次周へ
            logger.warning("poll ループ例外 (次周で再試行): %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
