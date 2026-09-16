# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""週間基本編成 (Series / SeriesSlot) の編集フォーム (#6 Phase C / Phase 2)。"""

from __future__ import annotations

from django import forms

from core.forms import BootstrapFormMixin
from scheduling.models import ExposurePolicy, ProgramType, RecurrenceKind, Series, SeriesSlot

DOW_CHOICES = [(0, "月"), (1, "火"), (2, "水"), (3, "木"), (4, "金"), (5, "土"), (6, "日")]


def _parse_int_csv(raw: str, *, lo: int, hi: int, label: str) -> list[int]:
    """'2,4' → [2,4]。範囲外/非数値は ValueError(メッセージ)。空は空リスト。"""
    out: list[int] = []
    for tok in (raw or "").replace("、", ",").replace(" ", "").split(","):
        if not tok:
            continue
        if not tok.lstrip("-").isdigit():
            raise ValueError(f"{label}: '{tok}' は数値ではありません")
        v = int(tok)
        if not (lo <= v <= hi):
            raise ValueError(f"{label}: {v} は {lo}〜{hi} の範囲外です")
        if v not in out:
            out.append(v)
    return out


class SeriesForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Series
        fields = [
            "title",
            "slug",
            "genre",
            "rating",
            "exposure_policy_default",
            "description",
            "cast",
            "thumbnail_url",
            "x_handle",
            "x_hashtag",
            "is_active",
            "clock_hidden",
            "lbar_hidden",
            # #23 YouTube 番組専用枠の既定。展開 Program はこの preset を継承する。
            "youtube_dedicated",
            "youtube_preset",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "cast": forms.Textarea(attrs={"rows": 2, "placeholder": "出演者 (カンマ/改行区切り)"}),
            "thumbnail_url": forms.URLInput(
                attrs={
                    "placeholder": "https:// (任意・推奨 PC 1920×720px / SP 1280×720px・被写体は横中央に)"
                }
            ),
            "slug": forms.TextInput(
                attrs={"placeholder": "例: morning-show (空=自動採番)", "pattern": r"[-a-z0-9_]+"}
            ),
            "x_handle": forms.TextInput(attrs={"placeholder": "@なし可、例: icsTV"}),
            "x_hashtag": forms.TextInput(attrs={"placeholder": "#なし可、例: ICS_TV朝"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # モデルは blank=False (常に4択のどれか) だが、旧フォーム(studio 導入前)からの POST は
        # この項目を送らないため、未送信時に空文字で上書きしないよう任意化 + clean で既定へ落とす。
        self.fields["exposure_policy_default"].required = False

    def clean_exposure_policy_default(self):
        return self.cleaned_data.get("exposure_policy_default") or ExposurePolicy.PUBLIC

    def clean_slug(self):
        slug = self.cleaned_data.get("slug", "").strip()
        if not slug:
            return ""
        from django.utils.text import slugify

        if slugify(slug) != slug:
            raise forms.ValidationError(
                "スラッグは半角英数・ハイフン・アンダースコアのみ使えます。"
            )
        if slug.isdigit():
            raise forms.ValidationError("数字のみのスラッグは使えません (ID と混同します)。")
        return slug


class SeriesSlotForm(BootstrapFormMixin, forms.ModelForm):
    """週間スロット。尺は分入力 (duration_ms に換算)。録画=既定素材必須 / 生=live_source 必須。"""

    dow = forms.TypedChoiceField(coerce=int, choices=DOW_CHOICES, label="曜日")
    duration_min = forms.IntegerField(min_value=1, label="尺(分)")
    # 省略時は WEEKLY (既存リンク/最小 POST の後方互換)。clean/save で WEEKLY に既定化。
    recurrence_kind = forms.ChoiceField(
        choices=RecurrenceKind.choices,
        initial=RecurrenceKind.WEEKLY,
        required=False,
        label="繰り返し",
    )
    # kind 別パラメタ (CSV 手入力)。clean で recurrence_param(JSON) に集約。
    weeks_csv = forms.CharField(required=False, label="第N (例 2,4)")
    days_csv = forms.CharField(required=False, label="日 (例 1,15)")
    ending_csv = forms.CharField(required=False, label="末尾 (例 5)")

    class Meta:
        model = SeriesSlot
        fields = [
            "dow",
            "start_time",
            "program_type",
            "default_asset",
            "live_source",
            "effective_from",
            "effective_to",
        ]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.duration_ms:
            self.fields["duration_min"].initial = self.instance.duration_ms // 60000
        # default_asset は録画向け (program/任意素材)、live_source は生向け。任意フィールド。
        self.fields["default_asset"].required = False
        self.fields["live_source"].required = False
        self.fields["effective_to"].required = False
        # 既存 instance の recurrence_param を CSV 初期値へ復元
        if self.instance and self.instance.pk:
            self.fields["recurrence_kind"].initial = self.instance.recurrence_kind
            p = self.instance.recurrence_param or {}
            self.fields["weeks_csv"].initial = ",".join(str(x) for x in p.get("weeks", []))
            self.fields["days_csv"].initial = ",".join(str(x) for x in p.get("days", []))
            self.fields["ending_csv"].initial = ",".join(str(x) for x in p.get("ending", []))

    def clean(self):
        c = super().clean()
        pt = c.get("program_type")
        # chk_slot_source: 録画は default_asset 必須・生は live_source 必須 (DB CHECK と一致)
        if pt == ProgramType.RECORDED and not c.get("default_asset"):
            self.add_error("default_asset", "録画スロットは既定素材が必須です")
        if pt == ProgramType.LIVE and not c.get("live_source"):
            self.add_error("live_source", "生スロットは live_source が必須です")
        # recurrence_kind 別に param を組み立て・検証 (未指定は毎週)
        kind = c.get("recurrence_kind") or RecurrenceKind.WEEKLY
        c["recurrence_kind"] = kind
        param: dict = {}
        try:
            if kind == RecurrenceKind.MONTHLY_NTH_DOW:
                weeks = _parse_int_csv(c.get("weeks_csv", ""), lo=1, hi=5, label="第N")
                if not weeks:
                    self.add_error("weeks_csv", "第N曜は週番号を1つ以上指定してください (例 2,4)")
                param["weeks"] = weeks
            elif kind == RecurrenceKind.DAYS_OF_MONTH:
                days = _parse_int_csv(c.get("days_csv", ""), lo=1, hi=31, label="日")
                if not days:
                    self.add_error("days_csv", "指定日は日付を1つ以上指定してください (例 1,15)")
                param["days"] = days
            elif kind == RecurrenceKind.DAYS_ENDING:
                ending = _parse_int_csv(c.get("ending_csv", ""), lo=0, hi=9, label="末尾")
                if not ending:
                    self.add_error("ending_csv", "末尾は1桁(0-9)を1つ以上指定してください (例 5)")
                param["ending"] = ending
        except ValueError as e:
            self.add_error(None, str(e))
        c["_recurrence_param"] = param
        return c

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.duration_ms = self.cleaned_data["duration_min"] * 60000
        obj.recurrence_kind = self.cleaned_data.get("recurrence_kind") or RecurrenceKind.WEEKLY
        obj.recurrence_param = self.cleaned_data.get("_recurrence_param", {})
        if commit:
            obj.save()
        return obj
