# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# Generated for icstv: stale normalize 回収用に processing 開始時刻を保持する。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('medialib', '0005_asset_thumbnail_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='asset',
            name='normalize_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
