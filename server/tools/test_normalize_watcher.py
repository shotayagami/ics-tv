# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""normalize_watcher の単体テスト (boto3/ffmpeg を偽装、Django 非依存)。

watcher は Docker では /app に mezz.py と並置される想定だが、テストは repo 配置から
importlib で読み込む。R2 は必要メソッドだけの偽クライアントに差し替える。
"""

from __future__ import annotations

import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_watcher():
    spec = importlib.util.spec_from_file_location(
        "normalize_watcher", os.path.join(_HERE, "normalize_watcher.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nw = _load_watcher()


class FakeClient:
    """boto3 s3 クライアントのうち watcher が使うメソッドだけを実装。"""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.uploaded: list[tuple] = []
        self.downloaded: list[tuple] = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.store[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": _Body(self.store[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = [k for k in self.store if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def download_file(self, Bucket, Key, filename):
        self.downloaded.append((Key, filename))
        with open(filename, "wb") as f:
            f.write(self.store.get(Key, b"srcbytes"))

    def upload_file(self, filename, Bucket, Key, ExtraArgs=None):
        self.uploaded.append((filename, Key))
        self.store[Key] = b"encoded-output"


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


BUCKET = "icstv-mezzanine"


def _req(asset_id=42, request_id="rid-1", **over):
    r = {
        "assetId": asset_id,
        "requestId": request_id,
        "sourceKey": "ingest/live_recording/ch1/42.mp4",
        "sourceDurationSec": 5400.0,
        "spec": {
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "vbitrate": "16M",
            "abitrate": "192k",
            "arate": "48000",
            "vcodec": "libx264",
            "preset": "veryfast",
            "acodec": "aac",
            "pix_fmt": "yuv420p",
            "profile": "high",
            "container": "mp4",
            "loudnorm_i": "-14",
            "loudnorm_lra": "11",
            "loudnorm_tp": "-1.5",
        },
    }
    r.update(over)
    return r


def test_heartbeat_puts_object():
    c = FakeClient()
    nw.heartbeat(c, BUCKET)
    assert nw.HEARTBEAT_KEY in c.store
    body = json.loads(c.store[nw.HEARTBEAT_KEY])
    assert body["version"] == nw.WATCH_VERSION


def test_needs_work_true_when_untouched():
    c = FakeClient()
    assert nw.needs_work(c, BUCKET, 42, "rid-1") is True


def test_needs_work_false_when_claimed():
    c = FakeClient()
    nw.put_json(c, BUCKET, nw.status_key(42), {"requestId": "rid-1", "phase": "encoding"})
    assert nw.needs_work(c, BUCKET, 42, "rid-1") is False


def test_needs_work_false_when_result_exists():
    c = FakeClient()
    nw.put_json(c, BUCKET, nw.result_key(42), {"requestId": "rid-1"})
    assert nw.needs_work(c, BUCKET, 42, "rid-1") is False


def test_needs_work_true_for_new_request_id():
    c = FakeClient()
    nw.put_json(c, BUCKET, nw.status_key(42), {"requestId": "rid-OLD", "phase": "failed"})
    assert nw.needs_work(c, BUCKET, 42, "rid-NEW") is True


def test_process_one_invalid_spec_fails():
    c = FakeClient()
    req = _req()
    req["spec"]["vcodec"] = "libx265"  # ホワイトリスト外
    nw.process_one(c, BUCKET, req)
    status = json.loads(c.store[nw.status_key(42)])
    assert status["phase"] == "failed"
    assert "invalid spec" in status["error"]
    assert nw.result_key(42) not in c.store  # 成果物は出ない


def test_process_one_success(monkeypatch):
    c = FakeClient()
    # loudnorm/ffmpeg を偽装 (実 ffmpeg を呼ばない)
    monkeypatch.setattr(nw.mezz, "loudnorm_first_pass", lambda src, spec, **kw: None)
    monkeypatch.setattr(nw, "_run_ffmpeg_with_progress", lambda *a, **kw: None)
    nw.process_one(c, BUCKET, _req())

    # status=encoding を経て result.json (完了マーカー) が最後に置かれる
    assert nw.mezz_out_key(42) in c.store  # upload された
    result = json.loads(c.store[nw.result_key(42)])
    assert result["requestId"] == "rid-1"
    assert result["sourceDurationSec"] == 5400.0
    assert c.downloaded and c.downloaded[0][0] == "ingest/live_recording/ch1/42.mp4"


def test_process_one_ffmpeg_failure_marks_failed(monkeypatch):
    import subprocess

    c = FakeClient()
    monkeypatch.setattr(nw.mezz, "loudnorm_first_pass", lambda src, spec, **kw: None)

    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"encode error")

    monkeypatch.setattr(nw, "_run_ffmpeg_with_progress", boom)
    nw.process_one(c, BUCKET, _req())
    status = json.loads(c.store[nw.status_key(42)])
    assert status["phase"] == "failed"
    assert "ffmpeg failed" in status["error"]
    assert nw.result_key(42) not in c.store


def test_poll_once_processes_pending(monkeypatch):
    c = FakeClient()
    nw.put_json(c, BUCKET, "normalize/in/42/request.json", _req())
    monkeypatch.setattr(nw.mezz, "loudnorm_first_pass", lambda src, spec, **kw: None)
    monkeypatch.setattr(nw, "_run_ffmpeg_with_progress", lambda *a, **kw: None)
    handled = nw.poll_once(c, BUCKET)
    assert handled == 1
    assert nw.result_key(42) in c.store
    # 2 周目は completed なので処理しない (冪等)
    assert nw.poll_once(c, BUCKET) == 0


def test_poll_once_skips_claimed(monkeypatch):
    c = FakeClient()
    nw.put_json(c, BUCKET, "normalize/in/42/request.json", _req())
    nw.put_json(c, BUCKET, nw.status_key(42), {"requestId": "rid-1", "phase": "encoding"})
    called = []
    monkeypatch.setattr(nw, "process_one", lambda *a, **kw: called.append(1))
    assert nw.poll_once(c, BUCKET) == 0
    assert called == []
