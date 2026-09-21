# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class FanclubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fanclub"
    verbose_name = "ファンクラブ (番組公式サイト)"
