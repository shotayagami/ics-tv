# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scheduling のシグナル: 回別素材の正規化完了 → 編成への反映 (#5 / docs/delivery.md)。

承認時に Episode.asset を確定した素材は PENDING (尺なし) のため、その時点では既に展開済みの
Program を差し替えられない。正規化が READY になった Asset を post_save で捕捉し、当該素材を指す
Episode があれば apply_episode_asset をコミット後に投入する (medialib は無改修=依存逆流なし)。
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from medialib.models import Asset, NormalizeStatus


@receiver(post_save, sender=Asset, dispatch_uid="scheduling_apply_episode_asset_on_ready")
def apply_episode_asset_on_ready(sender, instance: Asset, **kwargs) -> None:
    if instance.normalize_status != NormalizeStatus.READY:
        return
    # 遅延 import: app ready 後にモデル/タスクのロード順をずらす
    from scheduling.models import Episode

    if not Episode.objects.filter(asset=instance).exists():
        return
    from scheduling.tasks import apply_episode_asset

    pk = instance.pk
    transaction.on_commit(lambda: apply_episode_asset.delay(pk))
