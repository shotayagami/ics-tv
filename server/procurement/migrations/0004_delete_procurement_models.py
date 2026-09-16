# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""番組予算 (procurement) の 3 モデルを落とす (P4 第 4 単位・D037)。

既存の migration は 1 本も消さず、leaf に DeleteModel を足す方式 (先行 3 単位と同じ)。
削除順は app 内 FK の子から親へ: Payment (cost→DeliveryCost) → DeliveryCost → ProgramBudget。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0003_payment_created_by_programbudget_updated_at_and_more"),
    ]

    operations = [
        migrations.DeleteModel(name="Payment"),
        migrations.DeleteModel(name="DeliveryCost"),
        migrations.DeleteModel(name="ProgramBudget"),
    ]
