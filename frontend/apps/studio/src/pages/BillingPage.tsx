// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";

import { api } from "@icstv/api";
import { EmptyState, Notice, StatusBadge, StudioPage, type StatusTone } from "../atoms";

import type { BillingOut } from "../hooks";
import { yen } from "../hooks";

/** 請求 billing (#Phase2d-2)。状態遷移スライス: 月次締め → 発行 → 入金/失効。
 * close-month は警告ありで closed=false → force 確認して再送。各遷移後にダッシュボード再取得。
 * 業務エラー (BillingError) は 409 → メッセージ表示。 */
export function BillingPage() {
  const [d, setD] = useState<BillingOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/billing/dashboard")
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, []);
  useEffect(load, [load]);

  /** 状態遷移 POST 共通: ロック→実行→(成功で再取得)。409 等は detail を表示。 */
  async function op(fn: () => Promise<{ error?: unknown }>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const { error } = await fn();
      if (error) {
        const detail = (error as { detail?: string })?.detail;
        setErr(detail || "操作に失敗しました");
        return;
      }
      setMsg(okMsg);
      load();
    } catch {
      setErr("通信に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  async function closeMonth(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const year = Number(f.get("year"));
    const month = Number(f.get("month"));
    if (busy) return;
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const send = (force: boolean) =>
        api.POST("/api/v1/admin/billing/close-month", { body: { year, month, force } });
      let { data, error } = await send(false);
      if (error) {
        setErr((error as { detail?: string })?.detail || "締めに失敗しました");
        return;
      }
      if (data && !data.closed) {
        // 警告あり → force 確認
        if (!window.confirm(data.warnings.join("\n") + "\n\nforce で続行しますか？")) return;
        ({ data, error } = await send(true));
        if (error) {
          setErr((error as { detail?: string })?.detail || "締めに失敗しました");
          return;
        }
      }
      setMsg(data?.message || "締めました");
      load();
    } catch {
      setErr("通信に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  function issue(id: number) {
    op(() => api.POST("/api/v1/admin/billing/invoices/{invoice_id}/issue", { params: { path: { invoice_id: id } } }), "発行しました");
  }
  function voidInv(id: number) {
    if (!window.confirm("失効しますか？")) return;
    op(() => api.POST("/api/v1/admin/billing/invoices/{invoice_id}/void", { params: { path: { invoice_id: id } } }), "失効しました");
  }
  function pay(id: number, total: number) {
    const v = window.prompt("入金額", String(total));
    if (!v) return;
    op(
      () =>
        api.POST("/api/v1/admin/billing/invoices/{invoice_id}/pay", {
          params: { path: { invoice_id: id } },
          body: { paid_amount: Number(v), method: "" },
        }),
      "入金登録しました",
    );
  }

  function statusTone(s: string): StatusTone {
    if (s === "paid") return "ok";
    if (s === "void") return "danger";
    return "neutral";
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage title={<>請求 <span className="muted">/ 月次締め・請求書・入金</span></>}>
      {err && <Notice variant="error">{err}</Notice>}
      {msg && <Notice variant="success">{msg}</Notice>}

      <section className="card" style={{ marginBottom: "1rem" }}>
        <h2>月次締め</h2>
        <form onSubmit={closeMonth} style={{ display: "flex", gap: ".5rem", alignItems: "center" }}>
          <input type="number" name="year" defaultValue={2026} style={{ width: "6rem" }} />
          <input type="number" name="month" defaultValue={6} min={1} max={12} style={{ width: "4rem" }} />
          <button className="btn" type="submit" disabled={busy}>
            締める
          </button>
        </form>
        <table style={{ marginTop: ".6rem" }}>
          <thead>
            <tr>
              <th>期</th>
              <th>締め</th>
            </tr>
          </thead>
          <tbody>
            {d.periods.map((p, i) => (
              <tr key={i}>
                <td>
                  {p.year}-{p.month}
                </td>
                <td>{p.closed_at || <span className="muted">未締め</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>請求書</h2>
        {d.invoices.length === 0 ? (
          <p className="muted">請求書はまだありません。月次締めで生成されます。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>番号</th>
                <th>期</th>
                <th>請求先</th>
                <th style={{ textAlign: "right" }}>小計</th>
                <th style={{ textAlign: "right" }}>手数料</th>
                <th style={{ textAlign: "right" }}>税</th>
                <th style={{ textAlign: "right" }}>合計</th>
                <th>状態</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {d.invoices.map((inv) => (
                <tr key={inv.id}>
                  <td>{inv.invoice_number}</td>
                  <td>{inv.period}</td>
                  <td>{inv.advertiser}</td>
                  <td style={{ textAlign: "right" }}>{yen(inv.subtotal)}</td>
                  <td style={{ textAlign: "right" }}>{yen(inv.commission_amount)}</td>
                  <td style={{ textAlign: "right" }}>{yen(inv.tax_amount)}</td>
                  <td style={{ textAlign: "right" }}>{yen(inv.total)}</td>
                  <td><StatusBadge label={inv.status} tone={statusTone(inv.status)} /></td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {inv.status === "draft" && (
                      <button className="btn" disabled={busy} onClick={() => issue(inv.id)} style={{ fontSize: ".78rem", padding: ".15rem .4rem" }}>
                        発行
                      </button>
                    )}
                    {inv.status === "issued" && (
                      <>
                        <button className="btn" disabled={busy} onClick={() => pay(inv.id, inv.total)} style={{ fontSize: ".78rem", padding: ".15rem .4rem", marginRight: ".3rem" }}>
                          入金
                        </button>
                        <button className="btn" disabled={busy} onClick={() => voidInv(inv.id)} style={{ fontSize: ".78rem", padding: ".15rem .4rem", background: "var(--warn)" }}>
                          失効
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </StudioPage>
  );
}
