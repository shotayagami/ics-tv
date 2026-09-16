# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材・CM の専用編集画面 (#7) の ModelForm 群。Django admin 直編集を置換する。"""

from __future__ import annotations

from django import forms

from core.forms import BootstrapFormMixin
from medialib.models import Asset, CmBundle, CmCreative, FillerPlaylist


class AssetForm(BootstrapFormMixin, forms.ModelForm):
    """素材の基本属性。尺/解像度/codec 等は正規化が埋めるため編集対象外 (read-only 表示)。

    usable_as_program / usable_as_filler は「役割」: kind(主分類) と独立に、この素材を
    番組編成・フィラーの素材ピッカーへ出すかを個別に制御する (1素材を「フィラーかつ番組」に)。
    """

    class Meta:
        model = Asset
        fields = [
            "kind",
            "title",
            "thumbnail_url",
            "r2_key",
            "source_path",
            "usable_as_program",
            "usable_as_filler",
            "rerun_eligible",
        ]
        labels = {
            "usable_as_program": "番組として編成できる",
            "usable_as_filler": "フィラーとして使用できる",
            "rerun_eligible": "再放送として番組表/現在放送中に出す",
        }
        help_texts = {
            "usable_as_program": "ONで番組編成の素材ピッカーに表示 (主分類が番組でなくても可)。",
            "usable_as_filler": "ONでフィラープレイリストの素材ピッカーに表示 (主分類が番組でも可)。",
            "rerun_eligible": (
                "ONでフィラー送出中にこの素材のタイトルを視聴者向け面 (番組表/現在放送中) へ"
                "「再放送」として露出。局ID/プロモ等は OFF のまま (既定 OFF)。"
            ),
        }
        widgets = {"thumbnail_url": forms.URLInput(attrs={"placeholder": "https:// (任意)"})}


class CmCreativeForm(BootstrapFormMixin, forms.ModelForm):
    """CM 固有属性 (広告主/グリッド/キャンペーン/上限/考査)。asset は別途指定。

    thumbnail_url は素材(Asset)側の属性を当画面から編集するための補助フィールド (ビューが asset へ書戻し)。
    """

    thumbnail_url = forms.CharField(
        required=False,
        label="サムネ URL",
        widget=forms.URLInput(attrs={"placeholder": "https:// (任意)"}),
    )

    class Meta:
        model = CmCreative
        fields = [
            "advertiser",
            "grid",
            "campaign_start",
            "campaign_end",
            "max_airings",
            "screening_status",
        ]
        widgets = {
            "campaign_start": forms.DateInput(attrs={"type": "date"}),
            "campaign_end": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.asset_id:
            self.fields["thumbnail_url"].initial = self.instance.asset.thumbnail_url


class CmBundleForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CmBundle
        fields = ["name", "note"]


class FillerPlaylistForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FillerPlaylist
        fields = ["name"]
