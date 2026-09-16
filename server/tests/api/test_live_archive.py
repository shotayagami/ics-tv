# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""live番組の YouTube アーカイブ見逃し (#VOD-01 live): 解決ロジック + 公開導線。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from core.models import LiveSource
from scheduling.models import Program, ProgramType
from youtube.archive import archive_watch_url, recent_live_archives
from youtube.models import YoutubeConfig, YoutubeSlot, YtPrivacy, YtSlotStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _live_source():
    return LiveSource.objects.create(name="現場", rtmp_app="app", rtmp_key="key")


def _live_program(channel, *, ended_ago=timedelta(hours=2), title="生中継"):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        live_source=_live_source(),
        public_visible=True,
    )


def _config(channel, privacy=YtPrivacy.PUBLIC):
    return YoutubeConfig.objects.create(channel=channel, privacy=privacy)


def _slot(channel, program, *, status=YtSlotStatus.COMPLETE, broadcast_id="yt-abc"):
    # 番組開始を内包する 2h 枠
    ws = program.start_at - timedelta(minutes=10)
    return YoutubeSlot.objects.create(
        channel=channel,
        window_start=ws,
        window_end=ws + timedelta(hours=2),
        broadcast_id=broadcast_id,
        status=status,
    )


def test_archive_url_for_completed_slot(channel, db):
    _config(channel)
    p = _live_program(channel)
    _slot(channel, p)
    assert archive_watch_url(p) == "https://www.youtube.com/watch?v=yt-abc"


def test_archive_url_empty_without_slot(channel, db):
    _config(channel)
    p = _live_program(channel)
    assert archive_watch_url(p) == ""


def test_archive_url_empty_when_slot_not_complete(channel, db):
    _config(channel)
    p = _live_program(channel)
    _slot(channel, p, status=YtSlotStatus.LIVE)
    assert archive_watch_url(p) == ""


def test_archive_url_empty_when_private(channel, db):
    _config(channel, privacy=YtPrivacy.PRIVATE)
    p = _live_program(channel)
    _slot(channel, p)
    assert archive_watch_url(p) == ""


def test_archive_url_empty_without_config(channel, db):
    p = _live_program(channel)
    _slot(channel, p)
    assert archive_watch_url(p) == ""  # config 無し = 公開可否不明 → 出さない


def test_recent_live_archives_lists(channel, db):
    _config(channel)
    p = _live_program(channel, title="先週の生配信")
    _slot(channel, p)
    rows = recent_live_archives()
    assert len(rows) == 1 and rows[0]["program"].id == p.id


@_PUBLIC_HOST
def test_program_detail_shows_youtube_cta(http_client, channel, db):
    _config(channel)
    p = _live_program(channel)
    _slot(channel, p)
    # #Phase2c: 操作バー(CTA)は島が /api/v1/program/{id} から描画 → API で検証 (yt は外部リンク)。
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["cta"]["kind"] == "yt" and d["cta"]["external"] is True
    assert d["cta"]["url"] == "https://www.youtube.com/watch?v=yt-abc"


@_PUBLIC_HOST
def test_vod_list_includes_live_archive(http_client, channel, db):
    _config(channel)
    p = _live_program(channel, title="アーカイブ生配信")
    _slot(channel, p)
    # #Phase2c: 見逃し一覧は島が /api/v1/vod から描画 → live_archives を API で検証
    archives = http_client.get("/api/v1/vod").json()["live_archives"]
    assert any(c["title"] == "アーカイブ生配信" for c in archives)
