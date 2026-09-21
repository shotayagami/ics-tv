# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exposure_policy (#27) のライブ視聴ゲート (docs/site-only-broadcast.md §4.7・docs/fanclub.md §6.4)。

2 つの独立したエンタイトルメント軸を、番組単位でどちらも(該当する方だけ)満たす必要がある
「レベル軸の一本化」として扱う:

  - サイト会員軸: site_members/members_yt_site は本線 HLS もサイト会員限定 (entitlement
    "exclusive")。scheduling.vod.can_watch の VodVisibility.SUBSCRIBERS 判定
    (has_active_subscription) と同じ「サイト会員」の実体を使う (docs §1.1)。
  - ファンクラブ ティア軸: Program.fc_required_level (VOD 側の vod_visibility=fanclub と共有する
    フィールド。ライブ視聴は vod_visibility に関係なく、この値が設定されていれば常に適用される)。
    NULL=完全公開/0=無料会員以上/n=有料ティアn以上。判定は fanclub.services.can_view_level
    (fanclub app への import は関数内に閉じ、scheduling → fanclub の一方向のみ成立させる)。

hls_url_for() は「共有 Live Input (Channel.cf_playback_hls_url は全番組で同一URL) を誰に出すか」を
判定する唯一の入り口。呼び出し側は channel.cf_playback_hls_url を直接参照せず、必ずこの関数を経由する
(過去、api.routers.player のみがこのゲートを実装し core.now_playing.card がゲートし忘れる、という
「共有 Live Input がゲートされていない」事故が起きたため、判定+署名付与を一箇所に集約する)。
"""

from __future__ import annotations

from scheduling.models import ExposurePolicy, Program

_SITE_MEMBER_GATED_POLICIES = {ExposurePolicy.SITE_MEMBERS, ExposurePolicy.MEMBERS_YT_SITE}


def requires_site_member_gate(policy: str) -> bool:
    return policy in _SITE_MEMBER_GATED_POLICIES


def _site_gate_ok(program: Program | None, member) -> bool:
    from subscriptions.services import has_active_subscription

    policy = program.resolved_exposure_policy if program is not None else ExposurePolicy.PUBLIC
    if not requires_site_member_gate(policy):
        return True
    return has_active_subscription(member)


def _fc_gate_ok(program: Program | None, member) -> bool:
    if program is None or program.fc_required_level is None:
        return True
    from fanclub.services import can_view_level, creator_for_series

    creator = creator_for_series(program.series)
    return can_view_level(program.fc_required_level, member, creator)


def can_watch_live(program: Program | None, member) -> bool:
    """この会員 (None=未ログイン) が現在番組のライブ本線 HLS を視聴してよいか。

    program は現在番組 (無ければ None=完全公開扱い)。サイト会員軸・ファンクラブ軸の両方を満たす
    必要がある (どちらか一方だけが要求される場合はそちらのみが実質的なゲートになる)。
    """
    return _site_gate_ok(program, member) and _fc_gate_ok(program, member)


def gate_reason(program: Program | None, member) -> str:
    """視聴不可の理由。'' | 'login' | 'subscribe' | 'fc_join' | 'fc_unavailable'。

    UI の導線出し分け用 (vod.gate_reason / fanclub.services.fc_gate_reason と同語彙)。
    サイト会員軸を先に判定する (未ログインはどちらの軸でも 'login' で語彙上一致するため優先順位は
    実害が無い。両軸とも満たせないログイン済み会員は、まずサイト会員軸の理由を返す)。
    """
    if not _site_gate_ok(program, member):
        return "login" if not member else "subscribe"
    if program is not None and not _fc_gate_ok(program, member):
        from fanclub.services import creator_for_series, fc_gate_reason

        creator = creator_for_series(program.series)
        return fc_gate_reason(program.fc_required_level, member, creator)
    return ""


def hls_url_for(channel, program: Program | None, member) -> str | None:
    """視聴可能なら署名トークン付きの再生 URL を、不可なら None を返す。

    サイト会員軸・ファンクラブ ティア軸のどちらでゲートされていても同じ署名トークンを使う
    (トークンは channel 単位・期限付きで、ティア/会員の別を問わないため両軸で共用できる)。

    **完全公開の番組にも署名を付ける**。エッジ (Cloudflare Worker、deploy/cloudflare-worker-hls/)
    は「番組が今ゲート対象か」を判定できないため、トークン無しで再生できる経路を1つでも残すと
    エッジ強制そのものが成立しない (公開番組の時間帯に取得した無署名 URL を、後から始まる
    ゲート対象番組にそのまま使い回せる — docs/site-only-broadcast.md §5 リスク#3)。
    URL が視聴者ごとに変わる分の CDN キャッシュ効率は、Worker 側がオリジンへの取得時に
    token を除いた URL でキャッシュキーを正規化することで担保する。
    """
    base = channel.cf_playback_hls_url or None
    if base is None:
        return None
    if not can_watch_live(program, member):
        return None
    from core.hls_auth import sign_hls_token

    token = sign_hls_token(channel.slug)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"
