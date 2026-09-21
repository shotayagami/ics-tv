# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""見逃し配信 (VOD) のクエリ・可視性・再生URL解決 (#VOD-01)。

ソースは放送済み録画番組の正規化メザニン (Program.asset, R2)。録画/再パッケージは不要で、
R2 の期限付き署名 GET URL を <video> に直接渡して progressive 再生する (HTTP range でシーク可)。
可視性 (vod_visibility) で全員/会員限定/サブスク限定を出し分け、subscribers が会員限定
コンテンツ (feat_exclusive) の実体になる。配信権の期間/媒体管理 (RIGHTS-02) はここに乗せる。
"""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.utils import timezone

from fanclub.services import can_view_level, creator_for_series, fc_gate_reason
from medialib.models import NormalizeStatus
from scheduling.models import Program, ProgramType, VodVisibility
from subscriptions.services import has_active_subscription

# 署名 URL の有効期限: 尺の 2 倍を基本に、最低 1h / 最大 6h でクランプ。
# 視聴中に切れて range 再取得が失敗しない長さを確保しつつ、共有耐性のため過大にしない。
_URL_MIN_EXPIRES = 3600
_URL_MAX_EXPIRES = 6 * 3600


def available_vod_qs(now=None) -> QuerySet[Program]:
    """可視性に関わらず「見逃し再生できる状態」の番組 (新しい順)。

    条件: VOD 公開設定あり / 正規化済み素材あり (録画番組は asset、生放送は録画済み
    recording_asset) / 放送済み / 公開期間内。
    """
    now = now or timezone.now()
    recorded_ready = Q(
        type=ProgramType.RECORDED,
        asset__isnull=False,
        asset__normalize_status=NormalizeStatus.READY,
        asset__r2_key__isnull=False,
    ) & ~Q(asset__r2_key="")
    live_recorded_ready = Q(
        type=ProgramType.LIVE,
        recording_asset__isnull=False,
        recording_asset__normalize_status=NormalizeStatus.READY,
        recording_asset__r2_key__isnull=False,
    ) & ~Q(recording_asset__r2_key="")
    qs = (
        Program.objects.filter(recorded_ready | live_recorded_ready, end_at__lte=now)
        .exclude(vod_visibility=VodVisibility.OFF)
        .filter(_until_open(now))
    )
    # 配信権 (RIGHTS-02) で VOD 不可の番組を除外 (権利レコードが無ければ素通し)
    from rights.services import filter_vod_allowed

    qs = filter_vod_allowed(qs, now)
    return qs.select_related("asset", "recording_asset", "series", "channel").order_by("-end_at")


def _until_open(now):
    from django.db.models import Q

    return Q(vod_available_until__isnull=True) | Q(vod_available_until__gt=now)


def can_watch(program: Program, member, now=None) -> bool:
    """この会員 (None=未ログイン) が program の VOD を再生してよいか。

    年齢制限ゲート (#BILL-02) と可視性ゲート (公開範囲) の両方を満たす必要がある。
    """
    from scheduling.ratings import age_gate_reason

    if age_gate_reason(program, member, now):
        return False
    vis = program.vod_visibility
    if vis == VodVisibility.PUBLIC:
        return True
    if vis == VodVisibility.MEMBERS:
        return bool(member and member.is_verified)
    if vis == VodVisibility.SUBSCRIBERS:
        return has_active_subscription(member)
    if vis == VodVisibility.FANCLUB:
        creator = creator_for_series(program.series)
        return can_view_level(program.fc_required_level, member, creator)
    return False  # OFF / 未知


def gate_reason(program: Program, member, now=None) -> str:
    """再生不可の理由 ('age' / 'login' / 'verify' / 'subscribe' / '')。UI の導線出し分け用。

    年齢制限を優先して返す (年齢不足/年齢不明は可視性より先に塞ぐ)。
    """
    if can_watch(program, member, now):
        return ""
    from scheduling.ratings import age_gate_reason

    age = age_gate_reason(program, member, now)
    if age:
        return age
    vis = program.vod_visibility
    if vis == VodVisibility.MEMBERS:
        return "login" if not member else "verify"
    if vis == VodVisibility.SUBSCRIBERS:
        return "login" if not member else "subscribe"
    if vis == VodVisibility.FANCLUB:
        creator = creator_for_series(program.series)
        return fc_gate_reason(program.fc_required_level, member, creator)
    return ""


def playback_url_with_expiry(program: Program) -> tuple[str, int]:
    """R2 の期限付き署名 GET URL と有効秒数。呼び出し側で can_watch 済み前提。

    アプリは URL を保持せず再生直前に取り直すため、残り秒数を一緒に返す必要がある。
    クランプ規則を呼び出し側で再計算させないよう、URL と対で返すのはここだけにする。
    """
    from core import r2

    asset = program.playback_asset
    if asset is None:
        raise ValueError(f"program {program.pk} に素材がない")
    key = asset.r2_key
    if not key:
        raise ValueError(f"program {program.pk} の素材に r2_key がない")
    dur_s = (asset.duration_ms or 0) // 1000
    expires = max(_URL_MIN_EXPIRES, min(_URL_MAX_EXPIRES, dur_s * 2))
    return r2.presign_get(key, expires=expires), expires


def playback_url(program: Program) -> str:
    """R2 の期限付き署名 GET URL (progressive MP4)。呼び出し側で can_watch 済み前提。"""
    return playback_url_with_expiry(program)[0]
