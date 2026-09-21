# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_channel_clock_style"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClockStylePreset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, verbose_name="プリセット名")),
                ("style", models.JSONField(default=dict, verbose_name="スタイル辞書")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "時計スタイルプリセット",
                "verbose_name_plural": "時計スタイルプリセット",
                "ordering": ["name"],
                "db_table": "clock_style_preset",
            },
        ),
    ]
