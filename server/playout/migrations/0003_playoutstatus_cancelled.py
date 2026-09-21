# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PlayoutStatus に CANCELLED を追加。リゾルバ再解決で消えた event を agent に
tombstone=true として伝えるための状態。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("playout", "0002_playout_event_sync_seq"),
    ]

    operations = [
        migrations.AlterField(
            model_name="playoutevent",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "scheduled"),
                    ("executing", "executing"),
                    ("done", "done"),
                    ("skipped", "skipped"),
                    ("failed", "failed"),
                    ("cancelled", "cancelled"),
                ],
                default="scheduled",
                max_length=12,
            ),
        ),
    ]
