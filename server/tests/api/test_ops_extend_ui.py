# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""押え/巻き UI (操作 view + プレビュー + ダッシュボード表示) のテスト (#7 O-C)。"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from scheduling.models import Program, ProgramType


def _ls():
    from core.models import LiveSource

    return LiveSource.objects.create(name="src", rtmp_app="live", rtmp_key="ch1")


def _live_now(channel, end_delta_min=50):
    """いま放送中の live 番組 (start=now-10m, end=now+end_delta_min)。"""
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title="L-now",
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=end_delta_min),
        live_source=_ls(),
    )


def _prog_url(slug, pid, suffix):
    return f"/ops/ch/{slug}/program/{pid}/{suffix}"


def test_extend_requires_staff(http_client, channel):
    p = _live_now(channel)
    res = http_client.post(_prog_url(channel.slug, p.id, "extend/"), {"delta_ms": 600000})
    assert res.status_code == 302


def test_op_extend_success_no_following(staff_client, channel):
    p = _live_now(channel)
    res = staff_client.post(_prog_url(channel.slug, p.id, "extend/"), {"delta_ms": 600000})
    assert res.status_code == 200
    end_before = p.end_at
    p.refresh_from_db()
    assert p.end_at == end_before + timedelta(minutes=10)


def test_op_extend_rejected_409_when_cannot_absorb(staff_client, channel, asset_ready):
    p = _live_now(channel)
    # 直後に隙間なしで recorded を置く (吸収不能 → 409)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="B",
        start_at=p.end_at,
        end_at=p.end_at + timedelta(hours=1),
        asset=asset_ready,
    )
    res = staff_client.post(_prog_url(channel.slug, p.id, "extend/"), {"delta_ms": 600000})
    assert res.status_code == 409


def test_op_shorten_success(staff_client, channel):
    p = _live_now(channel)
    res = staff_client.post(_prog_url(channel.slug, p.id, "shorten/"), {"delta_ms": 300000})
    assert res.status_code == 200
    end_before = p.end_at
    p.refresh_from_db()
    assert p.end_at == end_before - timedelta(minutes=5)


def test_op_shorten_recorded_409(staff_client, channel, asset_ready):
    now = timezone.now()
    p = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="rec",
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=50),
        asset=asset_ready,
    )
    res = staff_client.post(_prog_url(channel.slug, p.id, "shorten/"), {"delta_ms": 300000})
    assert res.status_code == 409


def test_op_extend_other_channel_404(staff_client, channel, db):
    from core.models import Channel

    other = Channel.objects.create(name="ch2", slug="ch2", enabled=True)
    p = _live_now(other)
    res = staff_client.post(_prog_url(channel.slug, p.id, "extend/"), {"delta_ms": 600000})
    assert res.status_code == 404


def test_extend_preview_renders(staff_client, channel):
    p = _live_now(channel)
    res = staff_client.get(_prog_url(channel.slug, p.id, "extend-preview/"), {"delta_ms": 600000})
    assert res.status_code == 200
    assert "押え" in res.content.decode("utf-8")


def test_dashboard_shows_extend_controls_when_live(staff_client, channel):
    _live_now(channel)
    res = staff_client.get(f"/ops/ch/{channel.slug}/panel/now/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "押え" in body
    assert "+10分" in body


def test_timeline_shows_extend_buttons_for_live(staff_client, channel):
    _live_now(channel)  # 表示窓 [now-1h, now+24h) 内
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/timeline/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "押え+5" in body
    assert "巻き-5" in body


def test_timeline_no_extend_buttons_for_recorded(staff_client, channel, asset_ready):
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="rec",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/timeline/")
    assert res.status_code == 200
    assert "押え+5" not in res.content.decode("utf-8")
