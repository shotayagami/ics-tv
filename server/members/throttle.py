# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""認証系エンドポイントの Cookie 非依存レート制限 (H-1 / M-1)。

従来のログイン throttle は失敗回数を `request.session` に持っていたため、Cookie を送らない
だけで毎回まっさらな session になり無制限に総当たり/クレデンシャルスタッフィングできた
(docs/security-review.md H-1)。ここでは失敗回数を DB (AuthThrottle) に scope+key ごとに
集計し、Cookie/セッションに一切依存しない。key はメール (HMAC ハッシュ・平文非保存) と
送信元 IP の両系統で、どちらかがロックされたら弾く。

既存の members.codes と同じ「DB で試行を数える」方式に揃える (Redis 追加不要・全 gunicorn
ワーカー / Pod 共有・再起動耐性)。ウィンドウ内に上限到達で当該 scope+key はウィンドウ満了
まで locked。成功時は該当キーの行を消す (下記 clear)。エッジ (CF/WAF) の rate-limit は本層を
補完するもので、ここはアプリ層の最終防衛線。
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

# client_ip / hash_email は security ログとも共有するため core.request_meta に集約 (再エクスポート)。
from core.request_meta import client_ip, hash_email  # noqa: F401

# scope 定数 (AuthThrottle.scope)。email/member 系と IP 系を分けて別々に数える。
LOGIN = "login"  # ログイン失敗 (key=email ハッシュ)
LOGIN_IP = "login_ip"  # ログイン失敗 (key=送信元 IP)
TOTP = "totp_2fa"  # TOTP 第2要素の失敗 (key=member.pk)
TOTP_IP = "totp_2fa_ip"  # TOTP 第2要素の失敗 (key=送信元 IP)

# scope → (上限 setting 名, 上限既定, ウィンドウ setting 名, ウィンドウ既定秒)
_LIMITS: dict[str, tuple[str, int, str, int]] = {
    LOGIN: ("ICSTV_LOGIN_FAIL_MAX", 5, "ICSTV_LOGIN_FAIL_WINDOW", 900),
    LOGIN_IP: ("ICSTV_LOGIN_IP_FAIL_MAX", 20, "ICSTV_LOGIN_FAIL_WINDOW", 900),
    TOTP: ("ICSTV_TOTP_FAIL_MAX", 5, "ICSTV_TOTP_FAIL_WINDOW", 900),
    TOTP_IP: ("ICSTV_TOTP_IP_FAIL_MAX", 20, "ICSTV_TOTP_FAIL_WINDOW", 900),
}


def _limit(scope: str) -> tuple[int, int]:
    name_max, def_max, name_win, def_win = _LIMITS[scope]
    return int(getattr(settings, name_max, def_max)), int(getattr(settings, name_win, def_win))


def is_locked(scope: str, key: str) -> bool:
    """scope+key がウィンドウ内で上限到達済み (=これ以上の試行を拒否すべき) か。"""
    from members.models import AuthThrottle

    if not key:
        return False
    max_fails, window = _limit(scope)
    row = AuthThrottle.objects.filter(scope=scope, key=key).first()
    if row is None:
        return False
    if (timezone.now() - row.window_start).total_seconds() > window:
        return False  # ウィンドウ満了 = 実質リセット (次の失敗記録で window_start を巻き直す)
    return row.fail_count >= max_fails


def record_failure(scope: str, key: str) -> None:
    """失敗を 1 件記録する。ウィンドウ満了後の初失敗は window_start を現在へ巻き直す。"""
    from members.models import AuthThrottle

    if not key:
        return
    _, window = _limit(scope)
    now = timezone.now()
    with transaction.atomic():
        row, created = AuthThrottle.objects.select_for_update().get_or_create(
            scope=scope, key=key, defaults={"fail_count": 1, "window_start": now}
        )
        if created:
            return
        if (now - row.window_start).total_seconds() > window:
            row.fail_count = 1
            row.window_start = now
        else:
            row.fail_count += 1
        row.save(update_fields=["fail_count", "window_start", "updated_at"])


def clear(scope: str, key: str) -> None:
    """成功時にカウンタを消す (該当 scope+key のみ)。"""
    from members.models import AuthThrottle

    if not key:
        return
    AuthThrottle.objects.filter(scope=scope, key=key).delete()
