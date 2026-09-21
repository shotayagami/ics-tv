# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材パイプラインのトリガ (medialib signals + admin action) の単体テスト。

焦点: normalize_asset を投入する経路が存在し、source_path 付き新規 Asset の作成と
管理画面アクションの双方から enqueue されること。enqueue は on_commit に積まれるため
django_capture_on_commit_callbacks(execute=True) で実行を捕捉する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage

from medialib import mezz, tasks
from medialib import normalize as nm
from medialib.admin import AssetAdmin, enqueue_normalize
from medialib.models import Asset, AssetKind, NormalizeStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def captured_delay(monkeypatch):
    """normalize_asset.delay を記録するだけのスタブに差し替え (broker を叩かない)。"""
    calls: list[int] = []
    monkeypatch.setattr(tasks.normalize_asset, "delay", calls.append)
    return calls


def _asset(*, source_path=None, status=NormalizeStatus.PENDING, kind=AssetKind.PROGRAM) -> Asset:
    return Asset.objects.create(
        kind=kind,
        title="src",
        source_path=source_path,
        normalize_status=status,
    )


def test_create_with_source_path_enqueues(captured_delay, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        asset = _asset(source_path="/in/ep1.mov", status=NormalizeStatus.PENDING)
    assert captured_delay == [asset.pk]


def test_create_ready_asset_does_not_enqueue(captured_delay, django_capture_on_commit_callbacks):
    """既に READY (= 外部投入済み) の Asset は再正規化しない。"""
    with django_capture_on_commit_callbacks(execute=True):
        _asset(source_path="/in/ep1.mov", status=NormalizeStatus.READY)
    assert captured_delay == []


def test_create_without_source_path_does_not_enqueue(
    captured_delay, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        _asset(source_path=None, status=NormalizeStatus.PENDING)
    assert captured_delay == []


def test_update_does_not_enqueue(captured_delay, django_capture_on_commit_callbacks):
    """created=False の保存ではトリガしない (二重投入防止)。"""
    asset = _asset(source_path=None, status=NormalizeStatus.PENDING)
    captured_delay.clear()
    with django_capture_on_commit_callbacks(execute=True):
        asset.source_path = "/in/ep1.mov"
        asset.save()
    assert captured_delay == []


def _admin_request(rf):
    request = rf.post("/")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def test_admin_action_sets_pending_and_enqueues(
    rf, captured_delay, django_capture_on_commit_callbacks
):
    failed = _asset(source_path="/in/a.mov", status=NormalizeStatus.FAILED)
    ready = _asset(source_path="/in/b.mov", status=NormalizeStatus.READY)
    captured_delay.clear()  # 作成時 (FAILED/READY) はトリガしない

    admin_obj = AssetAdmin(Asset, AdminSite())
    qs = Asset.objects.filter(pk__in=[failed.pk, ready.pk])
    with django_capture_on_commit_callbacks(execute=True):
        enqueue_normalize(admin_obj, _admin_request(rf), qs)

    failed.refresh_from_db()
    ready.refresh_from_db()
    assert failed.normalize_status == NormalizeStatus.PENDING
    assert ready.normalize_status == NormalizeStatus.PENDING
    assert sorted(captured_delay) == sorted([failed.pk, ready.pk])


def test_passthrough_video_copy_sets_ready_and_flag(monkeypatch):
    """長尺 passthrough: 映像は -c:v copy (再エンコードせず)・status=READY・passthrough=True・原本解像度維持。"""
    monkeypatch.setattr(
        mezz,
        "loudnorm_first_pass",
        lambda src, spec, **kw: {
            "input_i": "-20",
            "input_tp": "-1",
            "input_lra": "7",
            "input_thresh": "-30",
            "target_offset": "0.1",
        },
    )
    captured: dict = {}
    monkeypatch.setattr(nm.subprocess, "run", lambda cmd, **kw: captured.update(cmd=cmd))
    monkeypatch.setattr(
        mezz,
        "ffprobe",
        lambda p, **kw: {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "30/1",
                    "codec_name": "h264",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        },
    )
    monkeypatch.setattr(
        nm.r2, "client", lambda: type("C", (), {"upload_fileobj": lambda *a, **k: None})()
    )
    monkeypatch.setattr(nm.r2, "bucket", lambda: "bucket")

    asset = _asset(source_path="/in/long.mov", status=NormalizeStatus.PROCESSING)
    nm._passthrough(asset, Path("/in/long.mov"), mezz.spec_from_env(), src_sec=3600.0)

    asset.refresh_from_db()
    assert asset.normalize_status == NormalizeStatus.READY
    assert asset.passthrough is True
    # ffmpeg は映像コピー (-c:v copy)、再エンコードしない
    assert (
        "-c:v" in captured["cmd"] and captured["cmd"][captured["cmd"].index("-c:v") + 1] == "copy"
    )
    assert (asset.width, asset.height) == (1280, 720)  # 原本解像度のまま
    assert "copy" in captured["cmd"] and "libx264" not in captured["cmd"]  # 映像 re-encode 無し
