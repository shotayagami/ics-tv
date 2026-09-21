# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""server の prefetch manifest 由来の per-channel メディア状態 (共有ホルダ)。

prefetch loop が manifest 受信時に slate_clip を更新し、slate 退避コード (feed_monitor /
dispatch) が参照する。manifest 未着 (起動直後/旧 server) や slate_asset 未設定なら既定
(config.slate_clip = ノードローカル please_wait) にフォールバックする。
"""

from __future__ import annotations


class ChannelMedia:
    def __init__(
        self,
        default_slate_clip: str,
        default_site_only_filler_clip: str = "",
        default_members_filler_clip: str = "",
    ) -> None:
        self._default_slate_clip = default_slate_clip
        self.slate_clip = default_slate_clip
        # exposure_policy (#27): YTミラー用の案内フィラー2種。slate と同型のフォールバック付き保持。
        self._default_site_only_filler_clip = default_site_only_filler_clip
        self._default_members_filler_clip = default_members_filler_clip
        self.site_only_filler_clip = default_site_only_filler_clip
        self.members_filler_clip = default_members_filler_clip

    def set_slate_clip(self, clip: str | None) -> None:
        """manifest の slate clip を反映する。None (slate_asset 未設定) なら既定へ戻す。"""
        self.slate_clip = clip or self._default_slate_clip

    def set_site_only_filler_clip(self, clip: str | None) -> None:
        """manifest の公開ミラー用案内フィラーを反映する。None なら既定へ戻す。"""
        self.site_only_filler_clip = clip or self._default_site_only_filler_clip

    def set_members_filler_clip(self, clip: str | None) -> None:
        """manifest のメンバーミラー用待機画を反映する。None なら既定へ戻す。"""
        self.members_filler_clip = clip or self._default_members_filler_clip
