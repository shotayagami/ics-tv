# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tools/spdx_headers.py (SPDX ライセンスヘッダの検査・付与) の単体テスト。

Django に依存しない stdlib のみのツールなのでパスから直接読み込み、tmp_path 上のファイルだけを触る。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "spdx_headers.py"
_spec = importlib.util.spec_from_file_location("spdx_headers", _TOOL)
assert _spec is not None and _spec.loader is not None
spdx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spdx)

HASH = "# SPDX-FileCopyrightText: 2026 アイシーエス\n# SPDX-License-Identifier: AGPL-3.0-or-later\n"
SLASH = (
    "// SPDX-FileCopyrightText: 2026 アイシーエス\n// SPDX-License-Identifier: AGPL-3.0-or-later\n"
)
BLOCK = (
    "/* SPDX-FileCopyrightText: 2026 アイシーエス */\n"
    "/* SPDX-License-Identifier: AGPL-3.0-or-later */\n"
)
SHEBANG = "#!/usr/bin/env python3\n"
CODING = "# -*- coding: utf-8 -*-\n"


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode())  # write_text は改行コードを変換しうるので bytes で書く
    return path


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        ("plain.py", "import os\n", HASH + "import os\n"),
        ("shebang.py", SHEBANG + "import os\n", SHEBANG + HASH + "import os\n"),
        ("coding.py", CODING + "x = 1\n", CODING + HASH + "x = 1\n"),
        ("both.py", SHEBANG + CODING + "x = 1\n", SHEBANG + CODING + HASH + "x = 1\n"),
        # coding 宣言の扱いは .py だけ
        ("enc.sh", CODING + "echo\n", HASH + CODING + "echo\n"),
        ("__init__.py", "", HASH),
        ("style.css", "body {}\n", BLOCK + "body {}\n"),
        ("app.ts", "export {};\n", SLASH + "export {};\n"),
        (
            "crlf.py",
            "import os\r\nx = 1\r\n",
            HASH.replace("\n", "\r\n") + "import os\r\nx = 1\r\n",
        ),
        # 末尾改行なしの shebang だけのファイル: 末尾改行なしのまま保つ
        ("run.sh", "#!/bin/sh", "#!/bin/sh\n" + HASH.removesuffix("\n")),
    ],
)
def test_fix_inserts_header_once(tmp_path, name, before, after):
    path = _write(tmp_path / name, before)
    assert spdx.main(["--fix", str(path)]) == 0
    assert path.read_bytes() == after.encode()
    # 冪等: 2 回目の --fix は何も変えず、--check も通る
    assert spdx.main(["--fix", str(path)]) == 0
    assert path.read_bytes() == after.encode()
    assert spdx.main(["--check", str(path)]) == 0


def test_check_fails_on_missing_header(tmp_path, capsys):
    path = _write(tmp_path / "a.py", "import os\n")
    assert spdx.main(["--check", str(path)]) == 1
    assert str(path) in capsys.readouterr().err
    assert path.read_bytes() == b"import os\n"


@pytest.mark.parametrize("line", HASH.splitlines(keepends=True))
def test_partial_header_reported_and_not_modified(tmp_path, capsys, line):
    before = line + "import os\n"
    path = _write(tmp_path / "a.py", before)
    assert spdx.main(["--fix", str(path)]) == 1
    assert str(path) in capsys.readouterr().err
    assert path.read_bytes() == before.encode()
    assert spdx.main(["--check", str(path)]) == 1


def test_out_of_scope_extension_ignored(tmp_path):
    path = _write(tmp_path / "notes.md", "# 見出し\n")
    assert spdx.main(["--check", str(path)]) == 0
    assert spdx.main(["--fix", str(path)]) == 0
    assert path.read_bytes() == "# 見出し\n".encode()
