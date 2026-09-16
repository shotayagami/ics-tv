# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0021_series_slug_x_audience_form"),
    ]

    operations = [
        migrations.AddField(
            model_name="series",
            name="lbar_hidden",
            field=models.BooleanField(default=False, verbose_name="L字を表示しない"),
        ),
        migrations.AddField(
            model_name="program",
            name="lbar_hidden",
            field=models.BooleanField(default=False, verbose_name="L字を表示しない"),
        ),
    ]
