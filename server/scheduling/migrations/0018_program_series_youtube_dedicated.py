# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#23 Program/Series に YouTube 番組専用枠フラグ (youtube_dedicated + youtube_preset) を追加。"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scheduling", "0017_earthquakealert"),
        ("youtube", "0008_youtubebroadcastpreset"),
    ]

    operations = [
        migrations.AddField(
            model_name="program",
            name="youtube_dedicated",
            field=models.BooleanField(default=False, verbose_name="YouTube 専用枠を立てる"),
        ),
        migrations.AddField(
            model_name="program",
            name="youtube_preset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="youtube.youtubebroadcastpreset",
            ),
        ),
        migrations.AddField(
            model_name="series",
            name="youtube_dedicated",
            field=models.BooleanField(default=False, verbose_name="YouTube 専用枠を立てる"),
        ),
        migrations.AddField(
            model_name="series",
            name="youtube_preset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="youtube.youtubebroadcastpreset",
            ),
        ),
    ]
