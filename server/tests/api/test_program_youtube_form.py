# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#23 ProgramForm / SeriesForm が YouTube 専用枠フラグを編集できることの確認。"""

from __future__ import annotations

import pytest
from django.utils import timezone

from youtube.models import YoutubeBroadcastPreset

pytestmark = pytest.mark.django_db


def test_program_form_exposes_youtube_fields():
    from scheduling.views import ProgramForm

    f = ProgramForm()
    assert "youtube_dedicated" in f.fields
    assert "youtube_preset" in f.fields
    assert f.fields["youtube_preset"].required is False


def test_series_form_exposes_youtube_fields():
    from scheduling.forms import SeriesForm

    f = SeriesForm()
    assert "youtube_dedicated" in f.fields
    assert "youtube_preset" in f.fields


def test_program_form_saves_dedicated_and_preset(channel, asset_ready):
    from scheduling.views import ProgramForm

    preset = YoutubeBroadcastPreset.objects.create(name="p")
    start = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M:%S")
    f = ProgramForm(
        {
            "title": "八神翔太の番組",
            "type": "recorded",
            "start_at": start,
            "asset": asset_ready.id,
            "public_visible": "on",
            "vod_visibility": "off",
            "youtube_dedicated": "on",
            "youtube_preset": preset.id,
        }
    )
    assert f.is_valid(), f.errors
    obj = f.save(commit=False)
    obj.channel = channel  # channel は view 側で設定 (form 外)
    obj.save()
    obj.refresh_from_db()
    assert obj.youtube_dedicated is True
    assert obj.youtube_preset_id == preset.id
    assert obj.wants_dedicated is True
    assert obj.resolved_youtube_preset == preset
