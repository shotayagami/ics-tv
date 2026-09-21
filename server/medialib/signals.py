# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""medialib のシグナル: 素材投入 → 正規化キューへの自動トリガ (docs/overview.md 3.2)。

これまで normalize_asset を呼ぶ経路が一切無く、素材パイプラインに入口が無かった。
source_path を持つ Asset が新規作成され、かつ未処理 (pending) のときに正規化を投入する。

enqueue は transaction.on_commit に積む:
- worker が row 確定前に走って DoesNotExist で落ちるのを防ぐ
- テスト (atomic ロールバック) では commit されないので broker を叩かない
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from medialib.models import Asset, NormalizeStatus


@receiver(post_save, sender=Asset, dispatch_uid="medialib_enqueue_normalize_on_create")
def enqueue_normalize_on_create(sender, instance: Asset, created: bool, **kwargs) -> None:
    if not created or not instance.source_path:
        return
    if instance.normalize_status != NormalizeStatus.PENDING:
        return

    # 遅延 import: tasks → normalize → models の読み込み順を app ready 後にずらす。
    # 入口は dispatch_normalize (Windows オフロード可否を判定し、不可ならローカル正規化を投入)。
    # オフロード無効時 (既定) は即ローカル正規化に落ちる (docs/normalize-offload.md)。
    from medialib.tasks import dispatch_normalize

    pk = instance.pk
    transaction.on_commit(lambda: dispatch_normalize.delay(pk))
