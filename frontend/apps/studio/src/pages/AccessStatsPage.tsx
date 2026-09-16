// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Meter, StudioPage } from "../atoms";

import type { AccessStatsOut, StatRow } from "../hooks";

const RANGE_OPTIONS: [number, string][] = [
  [24, "直近24時間"],
  [48, "直近48時間"],
  [24 * 7, "直近7日"],
  [24 * 30, "直近30日"],
];

function StatTable({ title, rows }: { title: string; rows: StatRow[] }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div className="card">
      <h3>{title}</h3>
      {rows.length === 0 && <p className="muted">データがありません。</p>}
      {rows.length > 0 && (
        <table>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <td style={{ width: "40%" }}>{r.label}</td>
                <td style={{ width: 60, textAlign: "right" }}>{r.count}</td>
                <td>
                  <Meter value={r.count} max={max} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** アクセス統計 (awstats 的な画面)。studio.* / backoffice.* 共通の単一画面
 * (backoffice はここへのリンクを持つだけで集計は複製しない)。 /api/v1/admin/access-stats。 */
export function AccessStatsPage() {
  const [sp, setSp] = useSearchParams();
  const qHost = sp.get("host") ?? "";
  const qRange = Number(sp.get("range_hours") ?? 24);
  const [d, setD] = useState<AccessStatsOut | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    setErr(false);
    api
      .GET("/api/v1/admin/access-stats", { params: { query: { host: qHost, range_hours: qRange } } })
      .then(({ data, error }) => (error ? setErr(true) : data && setD(data)))
      .catch(() => setErr(true));
  }, [qHost, qRange]);

  function selectHost(host: string) {
    setSp({ host, range_hours: String(qRange) });
  }

  function selectRange(range_hours: number) {
    setSp({ host: d?.host ?? qHost, range_hours: String(range_hours) });
  }

  if (err) return <EmptyState loading>読み込みに失敗しました (staff 権限が必要です)。</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage title="アクセス統計">
      <div className="card" style={{ marginBottom: "1rem", display: "flex", gap: "1.5rem", flexWrap: "wrap" }}>
        <span className="field">
          <label>ホスト</label>
          <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap" }}>
            {d.hosts.length === 0 && <span className="muted">記録がまだありません</span>}
            {d.hosts.map((h) => (
              <button
                key={h}
                type="button"
                className="btn"
                style={{ background: h === d.host ? undefined : "#444" }}
                onClick={() => selectHost(h)}
              >
                {h}
              </button>
            ))}
          </div>
        </span>
        <span className="field">
          <label>期間</label>
          <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap" }}>
            {RANGE_OPTIONS.map(([hours, label]) => (
              <button
                key={hours}
                type="button"
                className="btn"
                style={{ background: hours === d.range_hours ? undefined : "#444" }}
                onClick={() => selectRange(hours)}
              >
                {label}
              </button>
            ))}
          </div>
        </span>
      </div>

      <div className="st-kpis">
        <div className="st-kpi">
          <div className="n">{d.total_hits}</div>
          <div className="l">総ヒット数</div>
        </div>
        <div className="st-kpi">
          <div className="n">{d.total_pages}</div>
          <div className="l">うちページ表示 (API/WS/内部連携を除く)</div>
        </div>
      </div>

      <div className="st-grid">
        <StatTable title="時間帯別ヒット数" rows={d.hourly} />
        <StatTable title="ステータス内訳" rows={d.status} />
        <StatTable title="アクセス上位パス" rows={d.top_paths} />
      </div>
    </StudioPage>
  );
}
