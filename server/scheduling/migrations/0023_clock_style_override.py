# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0022_series_program_lbar_hidden"),
    ]

    operations = [
        migrations.AddField(
            model_name="series",
            name="clock_style_override",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="program",
            name="clock_style_override",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
