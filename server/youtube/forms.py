# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配信プリセット (#23) の studio フォーム。

YouTube Data API で反映できる項目を ModelForm で編集する。`tags` はカンマ区切り文字列
↔ list[str] に変換、`manual_checklist` (API 不可分の手動チェック項目) は view 側で
チェックボックス群として扱う (docs/youtube.md #23)。"""

from __future__ import annotations

from django import forms

from core.forms import BootstrapFormMixin
from medialib.models import Asset
from youtube.models import YoutubeBroadcastPreset

# YouTube 動画カテゴリ (videos.update snippet.categoryId)。配信設定画面の一覧に対応。
YT_CATEGORY_CHOICES = [
    ("", "（未設定）"),
    (24, "エンターテイメント"),
    (20, "ゲーム"),
    (23, "コメディ"),
    (17, "スポーツ"),
    (25, "ニュースと政治"),
    (26, "ハウツーとスタイル"),
    (22, "ブログ"),
    (15, "ペットと動物"),
    (1, "映画とアニメ"),
    (10, "音楽"),
    (28, "科学と技術"),
    (27, "教育"),
    (2, "自動車と乗り物"),
    (29, "非営利団体と社会活動"),
    (19, "旅行とイベント"),
]


class YoutubeBroadcastPresetForm(BootstrapFormMixin, forms.ModelForm):
    """API 反映項目の編集フォーム。tags はカンマ区切り、category は選択式。"""

    category_id = forms.TypedChoiceField(
        choices=YT_CATEGORY_CHOICES,
        coerce=int,
        required=False,
        empty_value=None,
        label="カテゴリ",
    )
    tags = forms.CharField(
        required=False,
        label="タグ (カンマ区切り)",
        widget=forms.TextInput(attrs={"placeholder": "VRChat, ゲーム実況"}),
    )

    class Meta:
        model = YoutubeBroadcastPreset
        fields = [
            "name",
            "channel",
            "title_template",
            "description_template",
            "category_id",
            "privacy",
            "made_for_kids",
            "default_language",
            "default_audio_language",
            "latency",
            "enable_dvr",
            "enable_embed",
            "enable_auto_start",
            "enable_auto_stop",
            "record_from_start",
            "license",
            "public_stats_viewable",
            "thumbnail",
            "playlist_id",
        ]
        widgets = {
            "description_template": forms.Textarea(attrs={"rows": 4}),
            "default_language": forms.TextInput(attrs={"placeholder": "ja"}),
            "default_audio_language": forms.TextInput(attrs={"placeholder": "ja"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].required = False
        self.fields["channel"].empty_label = "（全チャンネル共通）"
        self.fields["thumbnail"].required = False
        self.fields["thumbnail"].queryset = Asset.objects.order_by("title")
        # 既存 tags (list) を編集用にカンマ区切り文字列へ
        if self.instance and self.instance.pk and self.instance.tags:
            self.fields["tags"].initial = ", ".join(self.instance.tags)

    def clean_tags(self) -> list[str]:
        raw = (self.cleaned_data.get("tags") or "").strip()
        return [t.strip() for t in raw.split(",") if t.strip()]

    def save(self, commit: bool = True):
        obj = super().save(commit=False)
        obj.tags = self.cleaned_data.get("tags", [])
        if commit:
            obj.save()
        return obj
