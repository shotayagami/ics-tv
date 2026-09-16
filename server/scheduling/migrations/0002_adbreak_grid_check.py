# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations


class Migration(migrations.Migration):
    """ad_break.duration_ms を grid(15s=15000ms / 20s=20000ms)の整数倍に強制。

    剰余(%)条件は Django の CheckConstraint では表現しづらいため、DDL を直接付与して
    datamodel.md の chk_grid_multiple と完全一致させる。
    """

    dependencies = [
        ("scheduling", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE ad_break ADD CONSTRAINT chk_grid_multiple "
                "CHECK ((grid = '15s' AND duration_ms % 15000 = 0) "
                "OR (grid = '20s' AND duration_ms % 20000 = 0));"
            ),
            reverse_sql="ALTER TABLE ad_break DROP CONSTRAINT chk_grid_multiple;",
        ),
    ]
