// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import { EmptyState, Meter, StudioPage } from "../atoms";

import type { MemberStatsOut, StatRow } from "../hooks";

function StatTable({ title, rows, note }: { title: string; rows: StatRow[]; note?: string }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div className="card">
      <h3>{title}</h3>
      {note && (
        <p style={{ color: "var(--muted)", fontSize: "var(--fs-2xs)" }}>{note}</p>
      )}
      <table>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td style={{ width: "38%" }}>{r.label}</td>
              <td style={{ width: 52, textAlign: "right" }}>{r.count}</td>
              <td>
                <Meter value={r.count} max={max} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 会員統計 (#Phase2d 縦スライス)。/api/v1/admin/members/stats を staff session で取得。 */
export function MembersStats() {
  const [d, setD] = useState<MemberStatsOut | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    api
      .GET("/api/v1/admin/members/stats")
      .then(({ data, error }) => (error ? setErr(true) : data && setD(data)))
      .catch(() => setErr(true));
  }, []);

  if (err) return <EmptyState loading>読み込みに失敗しました (staff 権限が必要です)。</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;
  return (
    <StudioPage title="会員統計">
      <div className="st-kpis">
        <div className="st-kpi">
          <div className="n">{d.total}</div>
          <div className="l">会員数</div>
        </div>
        <div className="st-kpi">
          <div className="n">{d.verified}</div>
          <div className="l">本人確認済</div>
        </div>
        <div className="st-kpi">
          <div className="n">{d.unverified}</div>
          <div className="l">未確認</div>
        </div>
      </div>
      <div className="st-grid">
        <StatTable title="年齢層" rows={d.age} />
        <StatTable title="性別" rows={d.gender} />
        <StatTable title="2要素認証" rows={d.tfa} />
        <StatTable title="居住国" rows={d.country} />
        <StatTable
          title="地域 (郵便番号 上3桁・日本在住のみ)"
          rows={d.region}
          note={
            d.region_overseas_excluded > 0
              ? `海外在住 ${d.region_overseas_excluded} 名を除外しています。`
              : undefined
          }
        />
      </div>
    </StudioPage>
  );
}
