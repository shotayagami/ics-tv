# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DeliveryCost を delivery アプリへの FK から id 参照 + 名称スナップショットへ切り離す。

リファクタ Phase 3.8 (バックオフィス分離 / 納品サービス別リポ化の前提)。procurement→delivery の
DB レベル FK (delivery=PROTECT / vendor=SET_NULL) を解消し、delivery アプリ本体撤去 (Phase 3.9 /
delivery-service-split §11) をアンブロックする。

手順:
  1. 名称スナップショット列 (delivery_title / vendor_name) を追加。
  2. FK がまだ生きているうちに現行の納品タイトル / 制作会社名をバックフィル。
  3. AlterField(db_constraint=False) で FK 制約だけを落とす (カラム delivery_id / vendor_id と整数値は保持)。
  4. SeparateDatabaseAndState で state 上だけ FK→IntegerField に差し替え (DB 操作なし)。
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def backfill_snapshots(apps, schema_editor):
    DeliveryCost = apps.get_model("procurement", "DeliveryCost")
    Delivery = apps.get_model("delivery", "Delivery")
    Company = apps.get_model("delivery", "ProductionCompany")
    titles = dict(Delivery.objects.values_list("id", "title"))
    names = dict(Company.objects.values_list("id", "name"))
    for c in DeliveryCost.objects.all().only("id", "delivery_id", "vendor_id"):
        DeliveryCost.objects.filter(pk=c.pk).update(
            delivery_title=titles.get(c.delivery_id, ""),
            vendor_name=names.get(c.vendor_id, "") if c.vendor_id else "",
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0001_initial"),
        ("delivery", "0005_deliveryfile_qc_started_at"),
    ]

    operations = [
        # 1) 名称スナップショット列を追加
        migrations.AddField(
            model_name="deliverycost",
            name="delivery_title",
            field=models.CharField(default="", max_length=300),
        ),
        migrations.AddField(
            model_name="deliverycost",
            name="vendor_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        # 2) FK が生きているうちに名称をバックフィル
        migrations.RunPython(backfill_snapshots, noop),
        # 3) FK 制約だけ落とす (カラム delivery_id / vendor_id と整数データは保持)
        migrations.AlterField(
            model_name="deliverycost",
            name="delivery",
            field=models.ForeignKey(
                db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="costs",
                to="delivery.delivery",
            ),
        ),
        migrations.AlterField(
            model_name="deliverycost",
            name="vendor",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="costs",
                to="delivery.productioncompany",
            ),
        ),
        # 4) state だけ FK→IntegerField へ (カラムは既に制約無しの int・DB 操作なし)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="deliverycost", name="delivery"),
                migrations.RemoveField(model_name="deliverycost", name="vendor"),
                migrations.AddField(
                    model_name="deliverycost",
                    name="delivery_id",
                    field=models.IntegerField(default=0),
                    preserve_default=False,
                ),
                migrations.AddField(
                    model_name="deliverycost",
                    name="vendor_id",
                    field=models.IntegerField(blank=True, null=True),
                ),
            ],
            database_operations=[],
        ),
    ]
