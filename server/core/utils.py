# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""アプリ横断の小さな汎用ユーティリティ。単一責務のものだけをここに置く。"""

from typing import Any

from django.db import transaction


def stripe_to_dict(obj: Any) -> dict:
    """Stripe オブジェクトを入れ子まで plain dict にする。

    stripe>=12 の StripeObject は dict を継承しておらず `.get()` が AttributeError になる
    (添字 `obj["k"]` と属性 `obj.k` は使える)。v10 当時は dict だったため、素の
    StripeObject に `.get()` を使うコードが両アプリに散在していた。retrieve /
    construct_event の直後にここを通し、以降は素の dict として扱う。

    to_dict() は入れ子も plain dict に変換するため、`sub["items"]["data"][0]` のような
    多段アクセスも安全になる。既に dict のものはそのまま返す (べき等)。
    """
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


def move_ordered_item(item, siblings, direction: str, *, seq_field: str = "seq") -> None:
    """順序付き item を上下の隣接と seq 入替 (unique(parent, seq) を負値退避で回避)。

    medialib.CuePoint/CmBundleItem/FillerItem (旧 medialib.views._move_item) と scheduling.LiveCue
    が共有する idiom。siblings は同じ親配下の queryset。"""
    seq = getattr(item, seq_field)
    if direction == "up":
        nb = siblings.filter(**{f"{seq_field}__lt": seq}).order_by(f"-{seq_field}").first()
    elif direction == "down":
        nb = siblings.filter(**{f"{seq_field}__gt": seq}).order_by(seq_field).first()
    else:
        return
    if nb is None:
        return
    model = type(item)
    i_seq, n_seq = seq, getattr(nb, seq_field)
    with transaction.atomic():
        model.objects.filter(pk=item.pk).update(**{seq_field: -item.pk})
        model.objects.filter(pk=nb.pk).update(**{seq_field: i_seq})
        model.objects.filter(pk=item.pk).update(**{seq_field: n_seq})
