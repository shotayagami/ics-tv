# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scheduling"

    def ready(self):
        from scheduling import signals  # noqa: F401  (post_save receiver を登録)
