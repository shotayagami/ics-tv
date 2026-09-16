# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ソースファイルの SPDX ライセンスヘッダを検査・付与する (stdlib のみ)。

  python3 tools/spdx_headers.py --check [PATH ...]   ヘッダの無いファイルがあれば列挙して exit 1
  python3 tools/spdx_headers.py --fix [PATH ...]     無いファイルへ付与。片方だけのファイルは exit 1

PATH を省くと、リポジトリ直下 (このスクリプトの 1 つ上) で git ls-files した追跡ファイルのうち
配布範囲に入るものが対象。PATH を渡すとカレントディレクトリ基準で解決し、拡張子だけで絞る。

挿入位置: 1 行目が shebang ならその後ろ。.py は 1-2 行目の coding 宣言 (PEP 263) の後ろ
(宣言を 2 行目より下へ押し出さないため)。それ以外は先頭。改行コードと末尾改行の有無は保つ。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

# ---- 対象範囲: 無償配布ツリーの範囲と同じ定義を写したもの。範囲を変えるときは両方を揃える ----
INCLUDED_DIRS = (
    "server/",
    "frontend/",
    "agent/",
    "proto/",
    "deploy/playout-node/",
    "deploy/cloudflare-worker-hls/",
    "tools/",
    "docs/",
    ".github/",
)
ROOT_FILES = frozenset(
    {
        "docker-compose.yml",
        "README.md",
        "CONTRIBUTING.md",
        "openapi.json",
        ".gitignore",
        ".dockerignore",
        ".pre-commit-config.yaml",
        "buf.gen.yaml",
        "mkdocs.yml",
        "LICENSE",
        "SECURITY.md",
    }
)
EXCLUDED_DOCS = (
    "docs/hosts.md",
    "docs/infra-prerequisites.md",
    "docs/gitea-reconstruction.md",
    "docs/backup-dr.md",
    "docs/obs-live-streaming.md",
    "docs/security-review.md",
    "docs/runbook-*.md",
    "docs/audits/**",
    # 除去済み機能の設計書。配布しないので、ここでも範囲外にする。
    "docs/slidecast.md",
    "docs/weather-ingest.md",
    "docs/ranking.md",
)
# 拡張子 → コメント形式
EXTENSIONS = {
    ".py": "# {}",
    ".sh": "# {}",
    ".tsx": "// {}",
    ".ts": "// {}",
    ".js": "// {}",
    ".mjs": "// {}",
    ".proto": "// {}",
    ".scss": "/* {} */",
    ".css": "/* {} */",
}

COPYRIGHT = "SPDX-FileCopyrightText: 2026 アイシーエス"
LICENSE_ID = "SPDX-License-Identifier: AGPL-3.0-or-later"

REPO_ROOT = Path(__file__).resolve().parents[1]

_CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")


class PartialHeaderError(ValueError):
    """2 行のうち片方だけがある (どちらを足すべきか機械的に決めない)。"""


def in_scope(rel: str) -> bool:
    """リポジトリ直下からの相対パス (/ 区切り) が配布範囲の対象拡張子か。"""
    if not (rel.startswith(INCLUDED_DIRS) or rel in ROOT_FILES):
        return False
    if any(fnmatchcase(rel, pattern) for pattern in EXCLUDED_DOCS):
        return False
    return Path(rel).suffix in EXTENSIONS


def add_header(data: bytes, suffix: str) -> bytes | None:
    """ヘッダを足した内容を返す。先頭 4 行に 2 行とも揃っていれば None。"""
    first, second = (
        EXTENSIONS[suffix].format(t).encode() for t in (COPYRIGHT, LICENSE_ID)
    )
    lines = data.split(b"\n", 4)[:4]
    head = [line.removesuffix(b"\r") for line in lines]
    if first in head and second in head:
        return None
    if first in head or second in head:
        raise PartialHeaderError

    nl = b"\r\n" if b"\r\n" in data else b"\n"
    after = 1 if data.startswith(b"#!") else 0
    if suffix == ".py":
        for i, line in enumerate(lines[:2]):
            if _CODING_RE.match(line):
                after = i + 1
    header = first + nl + second

    offset = 0
    for _ in range(after):
        end = data.find(b"\n", offset)
        if end == -1:
            # 最終行 (shebang / coding) が改行なしで終わる: 末尾改行なしのまま後ろへ足す
            return data + nl + header
        offset = end + 1
    return data[:offset] + header + nl + data[offset:]


def _tracked_in_scope() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [rel for rel in os.fsdecode(out).split("\0") if rel and in_scope(rel)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPDX ライセンスヘッダの検査・付与")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="ヘッダの無いファイルを列挙して exit 1"
    )
    mode.add_argument(
        "--fix", action="store_true", help="ヘッダの無いファイルへ付与する"
    )
    parser.add_argument(
        "paths", nargs="*", metavar="PATH", help="省略時は配布範囲の追跡ファイル全部"
    )
    args = parser.parse_args(argv)

    if args.paths:
        targets = [(p, Path(p)) for p in args.paths if Path(p).suffix in EXTENSIONS]
    else:
        targets = [(rel, REPO_ROOT / rel) for rel in _tracked_in_scope()]
        if not targets:
            # 0 件で「全部付いている」と返さない (検査が空振りしても CI が緑になるため)
            print("対象ファイルが 0 件 (git ls-files の結果を確認)", file=sys.stderr)
            return 1

    missing: list[str] = []
    partial: list[str] = []
    for shown, path in targets:
        try:
            new = add_header(path.read_bytes(), path.suffix)
        except PartialHeaderError:
            partial.append(shown)
            continue
        if new is None:
            continue
        if args.fix:
            path.write_bytes(new)
            print(f"付与: {shown}")
        else:
            missing.append(shown)

    for shown in missing:
        print(f"SPDX ヘッダなし: {shown}", file=sys.stderr)
    for shown in partial:
        print(f"SPDX ヘッダが片方だけ (手で直す): {shown}", file=sys.stderr)
    if missing:
        print("python3 tools/spdx_headers.py --fix で付与できる", file=sys.stderr)
    return 1 if missing or partial else 0


if __name__ == "__main__":
    sys.exit(main())
