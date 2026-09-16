# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""本線 HLS の署名トークン (exposure_policy #27 / ファンクラブ ティア軸 docs/fanclub.md §6.4)。

docs/site-only-broadcast.md §4.7・リスク#3: プレイヤー UI で hls_url を隠すだけでは
manifest URL 流出で素通りになる。URL 自体に channel・期限付きの署名を埋め込み、エッジで検証する。

**エッジの実体は Cloudflare Worker** (`deploy/cloudflare-worker-hls/`)。本線ライブの実配信経路は
CF Stream ではなく送出ノードの nginx (ABR ladder) を k8s ingress-nginx 経由で出したもので、
その手前の Cloudflare に Worker を置いて `tv.yagamin.net/hls2/*` の全リクエストを検証する。
Worker が同じ HMAC を計算できるよう、署名鍵は `ICSTV_HLS_SIGNING_KEY` で Django と共有する
(未設定なら SECRET_KEY にフォールバック = 従来動作)。

トークンは「channel + 期限」しか束縛しない。番組やティアはエッジからは判定できないため、
**エンタイトルメントの変化 (番組がゲート対象へ切り替わる / 退会する) がエッジへ効くまでの最大遅延は
TTL そのもの**になる。アプリ層は資格のある視聴者にしかトークンを発行しないので、TTL を短くするほど
使い回しの窓が狭まる。TTL を短くしても視聴が途切れないよう、フロントは再取得したトークンを
再生を維持したまま差し替える (`@icstv/api` の hlsAuth / VideoPlayer・LiveBackground)。
"""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from django.utils import timezone

# 既定 TTL。settings.ICSTV_HLS_TOKEN_TTL_SEC で上書きできる。
DEFAULT_EXPIRES_SEC = 3600


def _secret() -> bytes:
    """署名鍵。エッジ (Cloudflare Worker) と共有するため SECRET_KEY とは分離できるようにする。

    分離しておけば Worker 側の secret が漏れても Django の SECRET_KEY (セッション/CSRF/暗号化
    フィールドの根) までは巻き込まれない。未設定時は従来どおり SECRET_KEY を使う。
    """
    key = getattr(settings, "ICSTV_HLS_SIGNING_KEY", "") or settings.SECRET_KEY or ""
    return key.encode()


def token_ttl_sec() -> int:
    return int(getattr(settings, "ICSTV_HLS_TOKEN_TTL_SEC", DEFAULT_EXPIRES_SEC))


def _signature(channel_slug: str, expires_at: int) -> str:
    msg = f"{channel_slug}:{expires_at}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def sign_hls_token(channel_slug: str, *, expires_sec: int | None = None) -> str:
    """channel_slug 限定・期限付きの HLS 再生トークンを発行する。"""
    ttl = token_ttl_sec() if expires_sec is None else expires_sec
    expires_at = int(timezone.now().timestamp()) + ttl
    return f"{expires_at}.{_signature(channel_slug, expires_at)}"


def verify_hls_token(channel_slug: str, token: str) -> bool:
    """署名トークンを検証する (channel 不一致/期限切れ/改竄はすべて拒否)。"""
    try:
        expires_str, sig = token.split(".", 1)
        expires_at = int(expires_str)
    except (ValueError, AttributeError):
        return False
    if timezone.now().timestamp() >= expires_at:
        return False
    return hmac.compare_digest(_signature(channel_slug, expires_at), sig)
