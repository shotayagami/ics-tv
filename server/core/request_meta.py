# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""リクエスト由来メタの共有ヘルパ (throttle / security_log が使う)。

client_ip はレート制限 (members.throttle) とセキュリティログ (core.security_log) の両方で
使うため 1 箇所に集約する。email 等は平文で残さないよう HMAC(SECRET_KEY) でハッシュ化する。
"""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings


def client_ip(request) -> str:
    """CF Tunnel → ingress 経由の実クライアント IP。

    既定は X-Forwarded-For の最左 (元クライアント)。プロキシ構成に応じ ICSTV_CLIENT_IP_HEADER
    (META キー) で上書き可 (本番は CF-Connecting-IP 相当を推奨)。ヘッダが無ければ REMOTE_ADDR。
    """
    if request is None:
        return ""
    header = getattr(settings, "ICSTV_CLIENT_IP_HEADER", "HTTP_X_FORWARDED_FOR")
    raw = request.META.get(header, "")
    if raw:
        return raw.split(",")[0].strip()[:64]
    return (request.META.get("REMOTE_ADDR") or "")[:64]


def hash_email(email: str) -> str:
    """メール等を平文で残さないための鍵付きハッシュ (SECRET_KEY で HMAC-SHA256)。

    正規化 (trim + lower) してからハッシュし、表記ゆれを束ねる。throttle のキーとログの
    email_hash に共通で使い、同一メールの失敗を横断集計しつつ平文は漏らさない。
    """
    secret = (settings.SECRET_KEY or "").encode()
    return hmac.new(secret, (email or "").strip().lower().encode(), hashlib.sha256).hexdigest()
