# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""agent ローカルテストの conftest。生成 proto を import path に追加。"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "icstv_proto"))
sys.path.insert(0, str(REPO_ROOT))
