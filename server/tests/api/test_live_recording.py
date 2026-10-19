# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生放送録画の所属照合。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from core.models import Channel, LiveSource
from medialib.models import Asset, AssetKind, NormalizeStatus
from playout.grpc_service import build_recording_upload
from scheduling import live_recording
from scheduling.models import (
    LiveRecordingIngest,
    LiveRecordingIngestState,
    Program,
    ProgramType,
)

pytestmark = pytest.mark.django_db


def _channel(slug: str) -> Channel:
    return Channel.objects.create(name=slug, slug=slug, enabled=True)


def _program(channel: Channel) -> Program:
    live_source = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="test")
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="生番組",
        start_at=start,
        end_at=start + timedelta(hours=1),
        live_source=live_source,
        record_live=True,
    )


def _asset(*, normalize_status=NormalizeStatus.READY) -> Asset:
    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="録画素材",
        source_path="r2://ingest/live_recording/ch1/1.mp4",
        normalize_status=normalize_status,
    )


def test_recording_upload_requires_program_to_belong_to_channel(channel, monkeypatch):
    other = _channel("ch2")
    program = _program(other)
    presign = Mock(return_value="signed-url")
    monkeypatch.setattr("core.r2.presign_put", presign)

    assert build_recording_upload(channel.slug, program.pk) is None
    assert build_recording_upload(other.slug, program.pk) == (
        f"ingest/live_recording/{other.slug}/{program.pk}.mp4",
        "signed-url",
    )
    presign.assert_called_once_with(
        f"ingest/live_recording/{other.slug}/{program.pk}.mp4", "video/mp4"
    )


def test_scan_leaves_r2_key_when_program_channel_does_not_match(channel, monkeypatch):
    other = _channel("ch2")
    program = _program(other)
    key = f"ingest/live_recording/{channel.slug}/{program.pk}.mp4"
    monkeypatch.setattr(live_recording.r2, "list_objects", lambda _prefix: [{"key": key}])

    assert live_recording.scan() == 0
    assert not LiveRecordingIngest.objects.filter(source_key=key).exists()
    assert not Asset.objects.filter(source_path=f"r2://{key}").exists()


def test_scan_ingests_matching_program(channel):
    program = _program(channel)
    key = f"ingest/live_recording/{channel.slug}/{program.pk}.mp4"
    from unittest.mock import patch

    with patch.object(live_recording.r2, "list_objects", return_value=[{"key": key}]):
        assert live_recording.scan() == 1

    ingest = LiveRecordingIngest.objects.get(source_key=key)
    assert ingest.channel == channel
    assert ingest.program == program
    assert ingest.state == LiveRecordingIngestState.NORMALIZING
    assert ingest.asset.source_path == f"r2://{key}"


def test_bind_rejects_existing_channel_mismatch_without_replacing_recording(channel):
    other = _channel("ch2")
    program = _program(other)
    existing_recording = _asset()
    program.recording_asset = existing_recording
    program.save(update_fields=["recording_asset"])
    asset = _asset()
    key = f"ingest/live_recording/{channel.slug}/{program.pk}.mp4"
    ingest = LiveRecordingIngest.objects.create(
        source_key=key,
        channel=channel,
        program=program,
        asset=asset,
        state=LiveRecordingIngestState.NORMALIZING,
    )

    assert live_recording.bind() == {"bound": 0, "failed": 1, "waiting": 0}
    ingest.refresh_from_db()
    program.refresh_from_db()
    assert ingest.state == LiveRecordingIngestState.FAILED
    assert ingest.asset_id == asset.pk
    assert ingest.source_key == key
    assert "所属不一致" in ingest.error
    assert program.recording_asset_id == existing_recording.pk
