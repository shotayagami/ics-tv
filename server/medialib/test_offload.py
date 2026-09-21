# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""正規化 Windows オフロード (medialib.offload + tasks の dispatch/reconcile) の単体テスト。

R2 はインメモリの偽ストアに差し替え、ffprobe/ffmpeg は monkeypatch する (実 R2/ffmpeg 不要)。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from medialib import mezz, offload, tasks
from medialib.models import Asset, AssetKind, NormalizeStatus

pytestmark = pytest.mark.django_db


class FakeR2:
    """core.r2 の必要関数だけを実装したインメモリストア。"""

    def __init__(self):
        self.store: dict[str, dict] = {}  # key -> {body, ct, last_modified}

    def put_object(self, key, body, content_type):
        self.store[key] = {
            "body": body if isinstance(body, bytes) else body.encode(),
            "ct": content_type,
            "last_modified": timezone.now(),
        }

    def put_json(self, key, obj, *, age_sec=0):
        self.store[key] = {
            "body": json.dumps(obj).encode(),
            "ct": "application/json",
            "last_modified": timezone.now() - timedelta(seconds=age_sec),
        }

    def get_object(self, key):
        if key not in self.store:
            raise KeyError(key)
        o = self.store[key]
        return o["body"], o["ct"]

    def head_object(self, key):
        o = self.store.get(key)
        if o is None:
            return None
        return {"last_modified": o["last_modified"], "size": len(o["body"])}

    def delete_object(self, key):
        self.store.pop(key, None)

    def list_objects(self, prefix):
        return [
            {"key": k, "size": len(v["body"]), "etag": "x", "last_modified": v["last_modified"]}
            for k, v in self.store.items()
            if k.startswith(prefix)
        ]

    def copy_object(self, src, dst, content_type=None):
        if src not in self.store:
            raise KeyError(src)
        self.store[dst] = dict(self.store[src])

    def presign_get(self, key, expires=600):
        return f"https://fake/{key}"

    def bucket(self):
        return "icstv-mezzanine"


@pytest.fixture
def fake_r2(monkeypatch):
    f = FakeR2()
    for name in (
        "put_object",
        "get_object",
        "head_object",
        "delete_object",
        "list_objects",
        "copy_object",
        "presign_get",
        "bucket",
    ):
        monkeypatch.setattr(f"core.r2.{name}", getattr(f, name))
    return f


@pytest.fixture
def offload_on(monkeypatch):
    monkeypatch.setenv("NORMALIZE_OFFLOAD_ENABLED", "true")
    monkeypatch.setenv("NORMALIZE_OFFLOAD_MIN_SOURCE_SEC", "300")


def _asset(
    *, source_path="r2://ingest/x/1.mp4", status=NormalizeStatus.PENDING, kind=AssetKind.PROGRAM
) -> Asset:
    return Asset.objects.create(
        kind=kind, title="t", source_path=source_path, normalize_status=status
    )


def _fresh_heartbeat(fake_r2):
    fake_r2.put_json(offload.HEARTBEAT_KEY, {"host": "win"}, age_sec=10)


def _mezz_probe(width=1920, height=1080, duration="600.0", vcodec="h264"):
    return {
        "format": {"duration": duration},
        "streams": [
            {
                "codec_type": "video",
                "width": width,
                "height": height,
                "avg_frame_rate": "60/1",
                "codec_name": vcodec,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }


# ---- heartbeat ----


def test_heartbeat_fresh_true_when_recent(fake_r2):
    fake_r2.put_json(offload.HEARTBEAT_KEY, {}, age_sec=10)
    assert offload.heartbeat_fresh() is True


def test_heartbeat_fresh_false_when_stale(fake_r2, monkeypatch):
    monkeypatch.setenv("NORMALIZE_OFFLOAD_HEARTBEAT_FRESH_SEC", "180")
    fake_r2.put_json(offload.HEARTBEAT_KEY, {}, age_sec=600)
    assert offload.heartbeat_fresh() is False


def test_heartbeat_fresh_false_when_absent(fake_r2):
    assert offload.heartbeat_fresh() is False


# ---- should_offload / dispatch ----


def test_dispatch_disabled_returns_local(fake_r2):
    a = _asset()
    assert offload.dispatch(a.id) == "disabled"
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.PENDING  # 状態を触らない


def test_dispatch_not_r2_source_local(fake_r2, offload_on):
    a = _asset(source_path="/local/in.mov")
    assert offload.dispatch(a.id) == "not-r2-source"


def test_dispatch_no_heartbeat_local(fake_r2, offload_on):
    a = _asset()  # heartbeat 無し
    assert offload.dispatch(a.id) == "no-heartbeat"


def test_dispatch_too_short_local(fake_r2, offload_on, monkeypatch):
    _fresh_heartbeat(fake_r2)
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: 180.0)
    a = _asset()
    assert offload.dispatch(a.id) == "too-short"


def test_dispatch_probe_none_local(fake_r2, offload_on, monkeypatch):
    _fresh_heartbeat(fake_r2)
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: None)
    a = _asset()
    assert offload.dispatch(a.id) == "probe-none"


def test_dispatch_offloads_long_clip(fake_r2, offload_on, monkeypatch):
    _fresh_heartbeat(fake_r2)
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: 5400.0)
    a = _asset()
    assert offload.dispatch(a.id) == "offload"
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.PROCESSING
    assert a.offload_request_id and a.offload_dispatched_at
    # request.json が in/ に置かれた
    req = fake_r2.store.get(offload.request_key(a.id))
    assert req is not None
    body = json.loads(req["body"])
    assert body["assetId"] == a.id
    assert body["sourceDurationSec"] == 5400.0
    assert body["requestId"] == a.offload_request_id
    assert body["spec"]["width"] == 1920


def test_dispatch_not_pending_skips(fake_r2, offload_on):
    a = _asset(status=NormalizeStatus.PROCESSING)
    assert offload.dispatch(a.id) == "not-pending"


# ---- dispatch_normalize task の振り分け ----


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_dispatch_task_local_reason_enqueues_normalize(fake_r2, monkeypatch):
    # disabled → LOCAL_REASONS → normalize_asset.delay 呼び出し
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    a = _asset()
    assert tasks.dispatch_normalize(a.id) == "disabled"
    assert calls == [a.id]


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_dispatch_task_offload_does_not_enqueue_normalize(fake_r2, offload_on, monkeypatch):
    _fresh_heartbeat(fake_r2)
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: 5400.0)
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    a = _asset()
    assert tasks.dispatch_normalize(a.id) == "offload"
    assert calls == []  # ローカルは投入しない


# ---- finalize ----


def _put_result(fake_r2, asset, rid, src_sec=600.0):
    fake_r2.put_object(offload.mezz_out_key(asset.id), b"video-bytes", "video/mp4")
    fake_r2.put_json(
        offload.result_key(asset.id),
        {"assetId": asset.id, "requestId": rid, "sourceDurationSec": src_sec},
    )


def test_finalize_validates_and_promotes(fake_r2, monkeypatch, django_capture_on_commit_callbacks):
    a = _asset(kind=AssetKind.FILLER)  # FILLER は字幕対象外 (whisper を呼ばない)
    a.normalize_status = NormalizeStatus.PROCESSING
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe(duration="600.5"))

    with django_capture_on_commit_callbacks(execute=True):
        out = tasks._reconcile_offload_one(a.id)

    assert out == "finalized"
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.READY
    assert a.r2_key == f"mezzanine/filler/{a.id}.mp4"
    assert a.width == 1920 and a.height == 1080
    assert a.offload_request_id is None  # 追跡クリア
    # mezzanine へ昇格 + out/in 掃除
    assert f"mezzanine/filler/{a.id}.mp4" in fake_r2.store
    assert offload.mezz_out_key(a.id) not in fake_r2.store
    assert offload.request_key(a.id) not in fake_r2.store


def test_finalize_rejects_wrong_resolution(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    # watcher が別 spec (1280x720) で焼いた → 検証NG → fallback
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe(width=1280, height=720))
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    a.refresh_from_db()
    assert a.offload_request_id is None
    assert calls == [a.id]  # ローカル再投入


def test_finalize_rejects_duration_mismatch(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    # 尺が原本より大きくずれる (300s vs 600s) → 検証NG
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe(duration="300.0"))
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    assert calls == [a.id]


def test_finalize_copy_failure_fallbacks(fake_r2, monkeypatch):
    """mezzanine への昇格 copy が失敗したら例外を伝播させず fallback (poison loop 回避)。"""
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe())

    def boom(src, dst, content_type=None):
        raise RuntimeError("R2 copy unsupported")

    monkeypatch.setattr("core.r2.copy_object", boom)
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.PROCESSING  # まだ READY にしない
    assert a.offload_request_id is None
    assert calls == [a.id]


def test_finalize_program_enqueues_captions(
    fake_r2, monkeypatch, django_capture_on_commit_callbacks
):
    a = _asset(kind=AssetKind.PROGRAM, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe())
    cap_calls = []
    monkeypatch.setattr(tasks.transcribe_asset, "delay", cap_calls.append)

    with django_capture_on_commit_callbacks(execute=True):
        out = tasks._reconcile_offload_one(a.id)

    assert out == "finalized"
    assert cap_calls == [a.id]  # 番組は字幕連鎖に乗る


# ---- reconcile 状態機械 (fallback 条件) ----


def _run_reconcile_one(asset_id):
    """on_commit (fallback の normalize_asset.delay 等) を実行しつつ _reconcile_offload_one を呼ぶ。"""
    from django.test import TestCase

    with TestCase.captureOnCommitCallbacks(execute=True):
        return tasks._reconcile_offload_one(asset_id)


def test_reconcile_no_claim_timeout_fallbacks(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=400)  # CLAIM=300 超
    a.save()
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    assert calls == [a.id]


def test_reconcile_waits_within_claim_window(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=60)  # まだ CLAIM 内
    a.save()
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "waiting"
    assert calls == []


def test_reconcile_watcher_failed_fallbacks(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=60)
    a.save()
    fake_r2.put_json(
        offload.status_key(a.id), {"requestId": "rid-1", "phase": "failed", "error": "boom"}
    )
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"


def test_reconcile_progress_stale_fallbacks(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=100)
    a.save()
    # encoding だが status 更新が PROGRESS_STALE (900s) 超で停止
    fake_r2.put_json(
        offload.status_key(a.id), {"requestId": "rid-1", "phase": "encoding"}, age_sec=1200
    )
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"


def test_reconcile_encoding_fresh_waits(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=100)
    a.save()
    fake_r2.put_json(
        offload.status_key(a.id), {"requestId": "rid-1", "phase": "encoding"}, age_sec=30
    )
    out = _run_reconcile_one(a.id)
    assert out == "waiting"


def test_reconcile_stale_result_requestid_ignored(fake_r2, monkeypatch):
    """旧 requestId の result は無視 (運用者が再正規化した後の遅延成果物)。"""
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-NEW"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=60)
    a.save()
    _put_result(fake_r2, a, "rid-OLD")  # 別 requestId
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: _mezz_probe())
    out = _run_reconcile_one(a.id)
    assert out == "waiting"  # finalize しない
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.PROCESSING


def test_reconcile_max_wait_fallbacks(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=20000)  # MAX_WAIT(14400)超
    a.save()
    # encoding が新しくても絶対上限で諦める
    fake_r2.put_json(
        offload.status_key(a.id), {"requestId": "rid-1", "phase": "encoding"}, age_sec=10
    )
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"


# ---- orphan 掃除 ----


def test_scan_orphans_deletes_old_inactive(fake_r2, monkeypatch):
    a = _asset(status=NormalizeStatus.READY)  # オフロード中でない
    # 古い成果物 (25h)
    old = timezone.now() - timedelta(hours=25)
    fake_r2.store[offload.mezz_out_key(a.id)] = {
        "body": b"x",
        "ct": "video/mp4",
        "last_modified": old,
    }
    fake_r2.store[offload.result_key(a.id)] = {
        "body": b"{}",
        "ct": "application/json",
        "last_modified": old,
    }
    deleted = offload.scan_orphans()
    assert deleted >= 2
    assert offload.mezz_out_key(a.id) not in fake_r2.store


def test_scan_orphans_skips_active_offload(fake_r2):
    a = _asset(status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.save()
    old = timezone.now() - timedelta(hours=25)
    fake_r2.store[offload.mezz_out_key(a.id)] = {
        "body": b"x",
        "ct": "video/mp4",
        "last_modified": old,
    }
    assert offload.scan_orphans() == 0  # 稼働中は触らない
    assert offload.mezz_out_key(a.id) in fake_r2.store


def test_scan_orphans_keeps_recent(fake_r2):
    a = _asset(status=NormalizeStatus.READY)
    fake_r2.store[offload.mezz_out_key(a.id)] = {
        "body": b"x",
        "ct": "video/mp4",
        "last_modified": timezone.now(),
    }
    assert offload.scan_orphans() == 0  # 新しいものは残す


def test_scan_orphans_reheads_and_skips_refreshed(fake_r2, monkeypatch):
    """list スナップショットは古いが、削除直前の再 HEAD で新しければ消さない (TOCTOU・#1)。"""
    a = _asset(status=NormalizeStatus.READY)
    key = offload.mezz_out_key(a.id)
    old = timezone.now() - timedelta(hours=25)
    fake_r2.store[key] = {"body": b"x", "ct": "video/mp4", "last_modified": old}

    # list は古いスナップショットを返すが、head は「今」上書きされた新しい時刻を返す。
    real_list = fake_r2.list_objects
    monkeypatch.setattr(
        "core.r2.head_object", lambda k: {"last_modified": timezone.now(), "size": 1}
    )
    monkeypatch.setattr("core.r2.list_objects", real_list)
    offload.scan_orphans()
    assert key in fake_r2.store  # 再更新済とみなし削除しない


# ---- 尺上限キャップ (#5) ----


def test_dispatch_too_long_passthrough_local(fake_r2, offload_on, monkeypatch):
    """MEZZ_MAX_SOURCE_SEC 超はローカル (即 passthrough) へ回し VOD 即時性を守る。"""
    _fresh_heartbeat(fake_r2)
    monkeypatch.setenv("MEZZ_MAX_SOURCE_SEC", "1800")
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: 7200.0)  # 2h > 30min
    a = _asset()
    assert offload.dispatch(a.id) == "too-long-passthrough"
    assert "too-long-passthrough" in offload.LOCAL_REASONS


def test_dispatch_offloads_mid_band_when_cap_set(fake_r2, offload_on, monkeypatch):
    """[MIN, MAX] 帯 (フルエンコードが遅い素材) はオフロード対象。"""
    _fresh_heartbeat(fake_r2)
    monkeypatch.setenv("MEZZ_MAX_SOURCE_SEC", "1800")
    monkeypatch.setattr(mezz, "probe_duration_sec", lambda url, **kw: 1200.0)  # 20min
    a = _asset()
    assert offload.dispatch(a.id) == "offload"


# ---- fps 検証 (#13) ----


def test_finalize_rejects_wrong_fps(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now()
    a.save()
    _put_result(fake_r2, a, "rid-1", src_sec=600.0)
    # 解像度/コーデック/尺は規格通りだが 30fps (規格 60fps) → 検証NG
    probe = _mezz_probe()
    probe["streams"][0]["avg_frame_rate"] = "30/1"
    monkeypatch.setattr(mezz, "ffprobe", lambda url, **kw: probe)
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    assert calls == [a.id]


# ---- fallback が normalize_started_at をリセット (#2/#12) ----


def test_fallback_resets_normalize_started_at(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(seconds=400)  # CLAIM 超 → fallback
    a.normalize_started_at = timezone.now() - timedelta(hours=5)  # 5h 前 (古い)
    a.save()
    monkeypatch.setattr(tasks.normalize_asset, "delay", lambda *_: None)
    out = _run_reconcile_one(a.id)
    assert out == "fallback"
    a.refresh_from_db()
    # 8h stale 回収に誤検出されないよう now にリセットされている
    assert (timezone.now() - a.normalize_started_at).total_seconds() < 60


# ---- dispatch_normalize が例外時ローカルへ (#8/#14) ----


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_dispatch_task_error_falls_back_local(fake_r2, monkeypatch):
    monkeypatch.setattr(
        offload, "dispatch", lambda aid: (_ for _ in ()).throw(RuntimeError("R2 down"))
    )
    calls = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    a = _asset()
    assert tasks.dispatch_normalize(a.id) == "dispatch-error-local"
    assert calls == [a.id]  # PENDING 放置せずローカルへ


# ---- reconcile_stale_normalize が offload 追跡を後始末 (#3) ----


def test_stale_normalize_clears_offload_tracking(fake_r2, monkeypatch):
    a = _asset(kind=AssetKind.FILLER, status=NormalizeStatus.PROCESSING)
    a.offload_request_id = "rid-1"
    a.offload_dispatched_at = timezone.now() - timedelta(hours=9)
    a.normalize_started_at = timezone.now() - timedelta(hours=9)  # 8h stale 超
    a.save()
    fake_r2.put_json(offload.request_key(a.id), {"assetId": a.id, "requestId": "rid-1"})
    n = tasks.reconcile_stale_normalize()
    assert n == 1
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.FAILED
    assert a.offload_request_id is None  # 追跡クリア
    assert offload.request_key(a.id) not in fake_r2.store  # request.json 削除


# ---- renormalize がオフロード追跡をクリア ----


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_renormalize_clears_offload_fields(
    fake_r2, monkeypatch, django_capture_on_commit_callbacks
):
    from medialib import services

    a = _asset(status=NormalizeStatus.FAILED)
    a.offload_request_id = "old-rid"
    a.offload_dispatched_at = timezone.now()
    a.save()
    monkeypatch.setattr(tasks.normalize_asset, "delay", lambda *_: None)
    with django_capture_on_commit_callbacks(execute=True):
        services.renormalize(a)
    a.refresh_from_db()
    assert a.normalize_status == NormalizeStatus.PENDING
    assert a.offload_request_id is None
    assert a.offload_dispatched_at is None
