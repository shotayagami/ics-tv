# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class MedialibConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "medialib"

    def ready(self) -> None:
        # post_save → 正規化キュー投入のシグナルを接続。
        from medialib import signals  # noqa: F401
