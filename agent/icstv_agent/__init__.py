# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ICS-TV playout agent.

クラウド送出ノードで動く独立プロセス。Django コントロールプレーンから gRPC で
PlayoutEvent を受け、ローカル AMCP (CasparCG) を駆動。as-run を返送する。
契約は proto/icstv/v1/playout.proto (言語非依存)。
"""

__version__ = "0.1.0"
