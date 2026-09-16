# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""共有フォームユーティリティ。Bootstrap 5 クラス付与ミックスイン等。"""

from __future__ import annotations

from django import forms

# form-control を付けるウィジェット型
_CONTROL = (
    forms.TextInput,
    forms.EmailInput,
    forms.PasswordInput,
    forms.NumberInput,
    forms.URLInput,
    forms.DateInput,
    forms.DateTimeInput,
    forms.TimeInput,
    forms.Textarea,
    forms.FileInput,
    forms.ClearableFileInput,
)
# form-select を付けるウィジェット型
_SELECT = (forms.Select, forms.SelectMultiple)


class BootstrapFormMixin:
    """Bootstrap 5 のフォームクラスを全ウィジェットに自動付与するミックスイン。

    使い方:
        class MyForm(BootstrapFormMixin, forms.ModelForm): ...

    付与ルール:
        テキスト系 / 日付 / ファイル → form-control
        Select 系               → form-select
        CheckboxInput           → form-check-input
        HiddenInput / その他     → スキップ
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, _CONTROL):
                _add_class(w, "form-control")
            elif isinstance(w, _SELECT):
                _add_class(w, "form-select")
            elif isinstance(w, forms.CheckboxInput):
                _add_class(w, "form-check-input")


def _add_class(widget: forms.Widget, cls: str) -> None:
    existing = widget.attrs.get("class", "")
    if cls not in existing.split():
        widget.attrs["class"] = (existing + " " + cls).strip()
