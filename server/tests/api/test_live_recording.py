# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生放送録画の所属照合。"""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

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
    key, url = build_recording_upload(other.slug, program.pk)
    key_pattern = (
        rf"ingest/live_recording/{other.slug}/{program.pk}/"
        r"\d{8}T\d{6}Z-[0-9a-f]{32}\.mp4"
    )
    assert re.fullmatch(key_pattern, key)
    assert live_recording.parse_key(key) == (other.slug, program.pk)
    assert url == "signed-url"
    presign.assert_called_once_with(key, "video/mp4")


def test_recording_upload_requires_record_live(channel, monkeypatch):
    program = _program(channel)
    program.record_live = False
    program.save(update_fields=["record_live"])
    presign = Mock()
    monkeypatch.setattr("core.r2.presign_put", presign)

    assert build_recording_upload(channel.slug, program.pk) is None
    presign.assert_not_called()


def test_recording_upload_requires_live_program(channel, monkeypatch):
    asset = _asset()
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    program = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="収録番組",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
    )
    presign = Mock()
    monkeypatch.setattr("core.r2.presign_put", presign)

    assert build_recording_upload(channel.slug, program.pk) is None
    presign.assert_not_called()


def test_recording_upload_requires_program_to_have_ended(channel, monkeypatch):
    program = _program(channel)
    now = timezone.now()
    program.start_at = now - timedelta(minutes=1)
    program.end_at = now + timedelta(minutes=1)
    program.save(update_fields=["start_at", "end_at"])
    presign = Mock()
    monkeypatch.setattr("core.r2.presign_put", presign)

    assert build_recording_upload(channel.slug, program.pk) is None
    presign.assert_not_called()


def test_recording_upload_uses_unique_generation_keys(channel, monkeypatch):
    program = _program(channel)
    monkeypatch.setattr("core.r2.presign_put", Mock(return_value="signed-url"))

    first = build_recording_upload(channel.slug, program.pk)[0]
    second = build_recording_upload(channel.slug, program.pk)[0]

    assert first != second
    assert live_recording.parse_key(first) == (channel.slug, program.pk)
    assert live_recording.parse_key(second) == (channel.slug, program.pk)


def test_parse_key_accepts_old_and_new_formats_only():
    old_key = "ingest/live_recording/ch1/42.mp4"
    new_key = "ingest/live_recording/ch1/42/20261118T123456Z-0123456789abcdef0123456789abcdef.mp4"

    assert live_recording.parse_key(old_key) == ("ch1", 42)
    assert live_recording.parse_key(new_key) == ("ch1", 42)
    assert live_recording.parse_key(new_key[:-5] + "g.mp4") is None
    assert live_recording.parse_key(new_key.replace("/42/", "/42/extra/")) is None


def test_scan_ingests_two_generations_for_one_program(channel, monkeypatch):
    program = _program(channel)
    keys = [live_recording.build_key(channel.slug, program.pk) for _ in range(2)]
    monkeypatch.setattr(
        live_recording.r2,
        "list_objects",
        lambda _prefix: [{"key": key} for key in keys],
    )

    assert live_recording.scan() == 2
    assert LiveRecordingIngest.objects.filter(source_key__in=keys).count() == 2
    assert Asset.objects.filter(source_path__in=[f"r2://{key}" for key in keys]).count() == 2


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
