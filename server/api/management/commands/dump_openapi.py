# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ninja API の OpenAPI スキーマを JSON 出力する。

フロントの TS クライアント生成元 (frontend が openapi-typescript で消費):

    python manage.py dump_openapi --out ../openapi.json
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from api.api import internal_admin_api, public_api


def _merged_schema() -> dict:
    """public_api と internal_admin_api の OpenAPI を 1 つに統合する (#sec M-4)。

    API を 2 分割してもフロントの TS クライアント生成元は 1 ファイルのままにするため、paths と
    components をマージする。両者の operationId/スキーマ名は元々一意 (別ルータ) なので衝突しない。

    components 配下の dict は **キーごとに** マージする必要がある。`{**cp, **ca}` だけだと
    admin 側の securitySchemes が public 側を丸ごと置き換え、視聴者向けの MemberAuth /
    MemberTokenAuth が定義されないまま operation からだけ参照される壊れたスキーマになる
    (TS の型生成は security を見ないので気付きにくいが、他言語のクライアント生成では効く)。
    """
    pub = dict(public_api.get_openapi_schema())
    adm = dict(internal_admin_api.get_openapi_schema())
    merged = dict(pub)
    merged["paths"] = {**pub.get("paths", {}), **adm.get("paths", {})}
    cp, ca = pub.get("components", {}), adm.get("components", {})
    components = {**cp, **ca}
    for key in set(cp) | set(ca):
        left, right = cp.get(key), ca.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            components[key] = {**left, **right}
    merged["components"] = components
    return merged


class Command(BaseCommand):
    help = "ninja API の OpenAPI スキーマを JSON 出力する (省略時 stdout)"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="", help="出力先パス (省略時 stdout)")

    def handle(self, *args, **options):
        schema = _merged_schema()
        text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
        out = options["out"]
        if out:
            Path(out).write_text(text + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"wrote {out}"))
        else:
            self.stdout.write(text)
