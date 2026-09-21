# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""YoutubeDescriptionTemplate マスタテーブルを追加。"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("youtube", "0010_youtubeconfig_slot_minutes_4h"),
    ]

    operations = [
        migrations.CreateModel(
            name="YoutubeDescriptionTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("title_template", models.CharField(blank=True, default="{program} | {channel}", max_length=300)),
                ("description_template", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "youtube_description_template",
                "ordering": ["name"],
            },
        ),
    ]
