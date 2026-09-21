// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import { EmptyState, StudioPage } from "../atoms";

import type { RightsDashboardOut } from "../hooks";

/** 権利ダッシュボード (#Phase2d 縦スライス)。期限切れ間近の配信権の一覧。 */
export function RightsDashboard() {
  const [d, setD] = useState<RightsDashboardOut | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    setD(null);
    setErr(false);
    api
      .GET("/api/v1/admin/rights/dashboard")
      .then(({ data, error }) => (error ? setErr(true) : data && setD(data)))
      .catch(() => setErr(true));
  }, []);

  return (
    <StudioPage title="権利・配信権">
      {err && <EmptyState loading>読み込みに失敗しました (staff 権限が必要です)。</EmptyState>}
      {!err && !d && <EmptyState loading>読み込み中…</EmptyState>}
      {d && (
        <>
          <h2>期限切れ間近の配信権 (30日以内)</h2>
          <table>
            <thead>
              <tr>
                <th>期限</th>
                <th>番組</th>
                <th>ch</th>
                <th>権利元</th>
              </tr>
            </thead>
            <tbody>
              {d.expiring.map((e, i) => (
                <tr key={i}>
                  <td>{e.available_until}</td>
                  <td>{e.program_title}</td>
                  <td>{e.channel}</td>
                  <td>{e.holder}</td>
                </tr>
              ))}
              {d.expiring.length === 0 && (
                <tr>
                  <td colSpan={4} className="muted">
                    期限切れ間近の配信権はありません。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </StudioPage>
  );
}
