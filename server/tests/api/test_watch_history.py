# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴履歴・続きから (#PERS-02): 進捗 upsert・完了判定・履歴一覧・VODのresume埋込。"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from members.models import Member, WatchHistory
from scheduling.models import Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_SIGNED = "https://r2.example/signed.mp4?sig=x"


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _program(
    channel, asset, *, vod=VodVisibility.PUBLIC, ended_ago=timedelta(hours=2), title="番組X"
):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        public_visible=True,
        vod_visibility=vod,
    )


@_PUBLIC_HOST
def test_progress_upsert(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    res = http_client.post(
        f"/members/watch/{p.id}/progress/", {"position_ms": 90000, "duration_ms": 3600000}
    )
    assert res.status_code == 204
    wh = WatchHistory.objects.get(member=m, program=p)
    assert wh.position_ms == 90000 and wh.completed is False
    # 同じ番組を再送 → 上書き (1 行のまま)
    http_client.post(
        f"/members/watch/{p.id}/progress/", {"position_ms": 120000, "duration_ms": 3600000}
    )
    assert WatchHistory.objects.filter(member=m, program=p).count() == 1
    wh.refresh_from_db()
    assert wh.position_ms == 120000


@_PUBLIC_HOST
def test_progress_marks_completed_near_end(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    http_client.post(
        f"/members/watch/{p.id}/progress/", {"position_ms": 3595000, "duration_ms": 3600000}
    )
    assert WatchHistory.objects.get(member=m, program=p).completed is True


@_PUBLIC_HOST
def test_progress_requires_login(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready)
    res = http_client.post(f"/members/watch/{p.id}/progress/", {"position_ms": 1000})
    assert res.status_code == 302 and res.url.startswith("/members/login/")
    assert not WatchHistory.objects.exists()


@_PUBLIC_HOST
def test_history_list_shows_watched(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready, title="見た番組")
    WatchHistory.objects.create(member=m, program=p, position_ms=1000, duration_ms=3600000)
    body = http_client.get("/members/history/").content.decode("utf-8")
    assert "見た番組" in body


@_PUBLIC_HOST
def test_vod_detail_embeds_resume(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    WatchHistory.objects.create(member=m, program=p, position_ms=45000, duration_ms=3600000)
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        body = http_client.get(f"/vod/{p.id}/").content.decode("utf-8")
    assert 'data-resume="45000"' in body
    assert f"/members/watch/{p.id}/progress/" in body


@_PUBLIC_HOST
def test_vod_detail_no_resume_when_completed(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready)
    WatchHistory.objects.create(
        member=m, program=p, position_ms=3600000, duration_ms=3600000, completed=True
    )
    with mock.patch("core.r2.presign_get", return_value=_SIGNED):
        body = http_client.get(f"/vod/{p.id}/").content.decode("utf-8")
    assert 'data-resume="0"' in body  # 完了済みは頭から
