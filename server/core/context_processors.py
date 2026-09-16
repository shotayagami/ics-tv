# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""全テンプレ共通の context。"""

from __future__ import annotations

from django.conf import settings


def version(request) -> dict:
    """フッタ等で使うアプリ版数 (settings.ICSTV_VERSION が単一ソース)。"""
    return {"app_version": settings.ICSTV_VERSION}


def public_base_url(request) -> dict:
    """公開サイトの絶対ベース URL (例 https://tv.yagamin.net)。

    管理ホストから公開ページへリンクする際や、公開テンプレのナビを必ず公開ホストへ向けるために使う。
    未設定 (ローカル等) は空文字 → テンプレ側で相対パスにフォールバック。
    """
    return {"public_base_url": getattr(settings, "ICSTV_PUBLIC_BASE_URL", "")}


def nav_defaults(request) -> dict:
    """管理コンソール共通ナビ用の channels / current_ch_slug を常時供給 (#7 ヘッダ統一)。

    ビューが値を渡せば view 側が優先される (Django は view context が context_processor を上書き)。
    渡さないビュー (請求/納品/admin-ui 等) でもナビが ch リンクを描けるよう、session の current_ch か
    先頭 enabled チャンネルを既定にする。auth 未設定や DB 未準備でも安全にフォールバック。
    """
    try:
        from core.models import Channel

        channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    except Exception:
        return {"channels": [], "current_ch_slug": None}
    slug = request.session.get("current_ch") if hasattr(request, "session") else None
    if not any(c.slug == slug for c in channels):
        slug = channels[0].slug if channels else None
    return {"channels": channels, "current_ch_slug": slug}
