// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link } from "react-router-dom";

import { StudioPage } from "../atoms";

import { NAV_GROUPS } from "../nav";

/** studio SPA ランディング。サイドバーと同じ 4 グループ (nav.ts NAV_GROUPS) の完全な
 * ショートカット一覧。宛先はサイドバーと単一正本を共有するのでドリフトしない。 */
export function Dashboard() {
  return (
    <StudioPage title="ダッシュボード">
      <p className="muted">各機能へのショートカット。メニューからも移動できます。</p>
      <div className="st-grid">
        {NAV_GROUPS.map((g) => (
          <div className="card" key={g.heading}>
            <h3 style={{ margin: "0 0 .5rem" }}>{g.heading}</h3>
            <nav className="st-launch">
              {g.items.map((it) => (
                <Link key={it.href} to={it.href}>
                  {it.label}
                </Link>
              ))}
            </nav>
          </div>
        ))}
      </div>
    </StudioPage>
  );
}
