# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Channel.agent_token を追加。agent gRPC Bearer (TODO: 暗号化保管)。"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="agent_token",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
    ]
