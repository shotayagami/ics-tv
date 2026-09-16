# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_chimesound_channelchime"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="clock_style",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
