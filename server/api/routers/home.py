# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開トップ ライブカード API (#Phase2b)。

ヒーロー(注目ch) + チャンネルカードのライブ状態 (online/live/現在番組/再生URL/poster) を返す。
home 島が初期描画 + 周期ポーリング (リロード無し更新) に使う。VOD/おすすめ/ジャンルは
Django SSR のままなので、ここは「ライブ部分」のデータのみ。会員がいれば pin 順に並べる。
core.views._home_columns / now_playing.card を再利用 (= /api/now と同じ now-playing 解決)。
"""

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Router

from api.schemas import HomeOut

router = Router(tags=["home"])


@router.get("/home", response=HomeOut, auth=None)
def home(request: HttpRequest, response: HttpResponse):
    from core import now_playing
    from core.views import _home_columns, _member_pinned_channel_ids

    # 署名付き再生URL・ゲート理由は視聴者ごとに異なるため、共有/CDNキャッシュに載せない
    # (#27 Phase B レビュー: channel_detail と同じ理由)。
    response["Cache-Control"] = "private, no-store"

    now = timezone.now()
    member = getattr(request, "member", None)
    cols = _home_columns(now)
    pinned = _member_pinned_channel_ids(member)
    if pinned:  # ピン留め ch を先頭へ (#EPG-04、安定ソートで slug 順維持)
        cols.sort(key=lambda c: c["channel"].id not in pinned)
    # card() は HomeCard の全フィールド (+ 余分な channel/current/upcoming) を持つ dict。ninja は余剰を無視。
    # member を渡さないと exposure_policy/ファンクラブ ティア軸のゲートが効かず、ホームの
    # ライブプレビューだけ非公開番組の hls_url が素通りする (#27 Phase B で修正した既知の不整合)。
    cards = [now_playing.card(c["channel"], now, c["current"], c["upcoming"], member) for c in cols]
    return {"featured": cards[0] if cards else None, "cards": cards}
