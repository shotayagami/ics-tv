# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""モバイルアプリ用 Bearer トークンの発行・検証・失効 (#MOBILE-01)。

Web の session cookie + CSRF は一切変更しない。これはネイティブアプリ専用の第2の入口で、
`members.auth` (session 版) と対になる。

平文トークンは発行時のレスポンスにしか現れず、DB には SHA-256 のみ置く。ログインのたびに
逐一照合するため、低速ハッシュ (make_password) ではなく SHA-256 を使う。総当たり耐性は
ハッシュの強度ではなく 256bit の乱数長で確保している。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone

# アプリは長期間ログインしたままになるため長め。失効はサーバ側の revoked_at で即座に効く。
TOKEN_TTL_DAYS = 90
# 第2要素の待ち時間。HTML 版のセッション pending より明示的に短くする (単回使用かつ短命)。
CHALLENGE_TTL_SECONDS = 10 * 60
# last_used_at の書き込み間隔。用途が粗い活動状況の把握なので、精度より書き込み量を優先する。
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(member, device_label: str = "") -> str:
    """トークンを発行し **平文** を返す。呼び出し側は第2要素まで通し終えていること。"""
    from members.models import MemberApiToken

    raw = secrets.token_urlsafe(32)  # 256bit
    MemberApiToken.objects.create(
        member=member,
        token_hash=_hash(raw),
        device_label=(device_label or "")[:64],
        expires_at=timezone.now() + timedelta(days=TOKEN_TTL_DAYS),
    )
    return raw


def resolve(raw: str | None):
    """平文トークンから会員を引く。無効なら None。

    last_used_at は update() で直接書く。ここは全 API リクエストの経路なので、save() で
    Member ごと触ったり signal を起こしたりしない。
    """
    if not raw:
        return None
    from members.models import MemberApiToken

    now = timezone.now()
    token = (
        MemberApiToken.objects.filter(
            token_hash=_hash(raw), revoked_at__isnull=True, expires_at__gt=now
        )
        .select_related("member")
        .first()
    )
    if token is None:
        return None
    if not token.member.is_active:
        return None
    # 「この端末が最近使われたか」を粗く知るための欄なので、毎リクエスト書かない。
    # 番組表を開いただけで数十リクエストが飛ぶため、素直に書くと閲覧と同じ量の UPDATE が出る。
    if token.last_used_at is None or (now - token.last_used_at) > LAST_USED_WRITE_INTERVAL:
        MemberApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token.member


def revoke(raw: str | None) -> bool:
    """このトークン 1 本だけ失効させる (他端末のログインは維持)。"""
    if not raw:
        return False
    from members.models import MemberApiToken

    updated = MemberApiToken.objects.filter(token_hash=_hash(raw), revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    return bool(updated)


def revoke_all(member) -> int:
    """その会員の全トークンを失効させる (パスワード変更・退会などから呼ぶ想定)。"""
    from members.models import MemberApiToken

    return MemberApiToken.objects.filter(member=member, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


# --- 第2要素の待ち状態 (session を持てないアプリ向け) ---
def start_challenge(member, method: str, device_label: str = ""):
    """第2要素待ちを作り、challenge を返す。トークンはまだ発行しない。"""
    from members.models import MemberLoginChallenge

    return MemberLoginChallenge.objects.create(
        member=member,
        method=method,
        device_label=(device_label or "")[:64],
        expires_at=timezone.now() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    )


def get_challenge(challenge_id):
    """未消費かつ期限内の challenge。無ければ None。"""
    from members.models import MemberLoginChallenge

    if not challenge_id:
        return None
    return (
        MemberLoginChallenge.objects.filter(
            challenge_id=challenge_id, consumed_at__isnull=True, expires_at__gt=timezone.now()
        )
        .select_related("member")
        .first()
    )


def consume_challenge(challenge) -> bool:
    """challenge を単回使用として畳む。

    条件付き UPDATE の戻り行数で判定するため、同じ challenge に対する並行リクエストの
    どちらか一方しか成功しない (トークンの二重発行を防ぐ)。
    """
    from members.models import MemberLoginChallenge

    updated = MemberLoginChallenge.objects.filter(pk=challenge.pk, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )
    return bool(updated)
