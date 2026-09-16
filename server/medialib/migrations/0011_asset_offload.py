# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""正規化 Windows オフロードの追跡フィールド (docs/normalize-offload.md §3.1)。"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("medialib", "0010_asset_captions"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="offload_request_id",
            field=models.CharField(blank=True, max_length=36, null=True),
        ),
        migrations.AddField(
            model_name="asset",
            name="offload_dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
