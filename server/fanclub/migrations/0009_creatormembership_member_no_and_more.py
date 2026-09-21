# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


def backfill_member_no(apps, schema_editor):
    """既存在籍分に会員番号を採番する (#27 §3.1 デジタル会員証)。

    creator 単位に joined_at → id 昇順 = 実際の加入順で 1 から振る。表示時の遅延採番
    (fanclub.services.ensure_member_no) だけに任せると「先に会員証を開いた人」の順になり、
    加入順と食い違った番号が永久に残るため、ここで確定させる。
    退会済みの行も採番する (再加入時に同じ番号を引き継ぐ設計のため)。
    """
    CreatorMembership = apps.get_model("fanclub", "CreatorMembership")
    creator_ids = (
        CreatorMembership.objects.order_by("creator_id")
        .values_list("creator_id", flat=True)
        .distinct()
    )
    for creator_id in list(creator_ids):
        rows = CreatorMembership.objects.filter(creator_id=creator_id).order_by("joined_at", "id")
        for no, row in enumerate(rows, start=1):
            if row.member_no != no:
                row.member_no = no
                row.save(update_fields=["member_no"])


class Migration(migrations.Migration):
    dependencies = [
        ("fanclub", "0008_creatormembership_pending_tier_and_more"),
        ("members", "0012_memberapitoken_memberloginchallenge"),
    ]

    operations = [
        migrations.AddField(
            model_name="creatormembership",
            name="member_no",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_member_no, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="creatormembership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("member_no__isnull", False)),
                fields=("creator", "member_no"),
                name="uq_creator_membership_member_no",
            ),
        ),
    ]
