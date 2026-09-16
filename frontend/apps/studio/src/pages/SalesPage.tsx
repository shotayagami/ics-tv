// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Notice, OldScreenLink, StudioPage } from "../atoms";

import type { AllocationOut } from "../hooks";
import { postForm } from "../hooks";

/** 営業 CM割付 sales (#Phase2d-9 / 2e-3)。日付/チャンネル単位で 番組→CM枠→配置CM を概観 + 自動補充。
 * 未送出枠は CM 入替(swap) ドロップダウンで差替可 (送出済は S11 不変で 409)。線引き(band) は
 * ドラッグ中心で複雑なため旧 allocation 画面 (edit_url) へ据え置き。 */
export function SalesPage() {
  const [sp, setSp] = useSearchParams();
  const channel = sp.get("channel") ?? "";
  const date = sp.get("date") ?? "";
  const [d, setD] = useState<AllocationOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    const query: Record<string, string | number> = {};
    if (channel) query.channel = Number(channel);
    if (date) query.date = date;
    api
      .GET("/api/v1/admin/sales/allocation", { params: { query } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [channel, date]);
  useEffect(load, [load]);

  function setFilter(key: string, v: string) {
    const next = new URLSearchParams(sp);
    if (v) next.set(key, v);
    else next.delete(key);
    setSp(next);
  }

  async function refill() {
    if (busy || !d?.channel_id) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status } = await postForm("/sales/allocation/refill/", { channel: String(d.channel_id) });
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg("未送出枠を再充填しました");
      load();
    } else setErr("再充填に失敗しました");
  }

  async function swap(itemId: number, cmAssetId: string) {
    if (busy || !cmAssetId) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status, data } = await postForm(`/sales/items/${itemId}/swap/`, { cm_asset_id: cmAssetId });
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg("CM を入替えました");
      load();
    } else setErr(typeof data === "string" ? data : "入替に失敗しました (送出済の枠は不可)");
  }

  async function addItem(breakId: number, cmId: string) {
    if (busy || !cmId) return;
    setBusy(true);
    setErr("");
    setMsg("");
    // 既存 adbreak_item_add を form-POST 再利用 (cm_id=CmCreative pk=cm_asset_id・grid 不一致は無視される)。
    const { status } = await postForm(`/scheduling/adbreaks/${breakId}/items/add/`, { cm_id: cmId });
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg("CM を追加しました");
      load();
    } else setErr("追加に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="CM 割付"
      actions={
        <>
          <input type="date" value={d.day} onChange={(e) => setFilter("date", e.target.value)} />
          <select value={String(d.channel_id ?? "")} onChange={(e) => setFilter("channel", e.target.value)}>
            {d.channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button className="btn" type="button" disabled={busy || !d.channel_id} onClick={refill}>自動補充</button>
          <OldScreenLink href={d.edit_url}>線引き (band)</OldScreenLink>
        </>
      }
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      {d.rows.length === 0 && <p className="muted">{d.channel_name} の {d.day} に番組がありません。</p>}
      {d.rows.map((p, pi) => (
        <section key={pi} className="card" style={{ marginBottom: ".7rem" }}>
          <h3 style={{ margin: "0 0 .3rem" }}>
            <span className="muted tabnum">{p.time}</span> {p.title}
          </h3>
          {p.breaks.length === 0 && <span className="muted" style={{ fontSize: ".8rem" }}>CM枠なし</span>}
          {p.breaks.map((b, bi) => (
            <div key={bi} style={{ display: "flex", gap: ".5rem", alignItems: "center", padding: ".15rem 0", flexWrap: "wrap" }}>
              <span className="muted tabnum" style={{ minWidth: "3.5rem", fontSize: ".8rem" }}>+{b.offset}</span>
              {b.items.length === 0 && <span className="muted" style={{ fontSize: ".8rem" }}>—</span>}
              {(() => {
                const opts = d.cm_options.filter((c) => c.grid === b.grid);
                return b.items.map((it) => (
                <span key={it.item_id} style={{ fontSize: ".8rem", padding: ".05rem .4rem", borderRadius: 3, border: "1px solid var(--line)", background: it.advertiser ? "var(--row)" : "transparent", color: it.advertiser ? "var(--fg)" : "var(--muted)", opacity: it.editable ? 1 : 0.7 }} title={it.match ? `placement: ${it.match}${it.editable ? "" : " (送出確定)"}` : ""}>
                  {it.advertiser || "空き枠"}
                  {it.match && <span className="muted" style={{ marginLeft: ".25rem", fontSize: ".7rem" }}>{it.match}</span>}
                  {it.editable && opts.length > 0 && (
                    <select value="" disabled={busy} onChange={(e) => swap(it.item_id, e.target.value)} title={`CM を入替 (${b.grid} 枠・送出前のみ)`} style={{ marginLeft: ".3rem", fontSize: ".7rem", padding: 0, maxWidth: "7rem", background: "transparent", border: "none", color: "var(--accent)", cursor: "pointer" }}>
                      <option value="">↻</option>
                      {opts.map((c) => <option key={c.id} value={String(c.id)}>{c.name}</option>)}
                    </select>
                  )}
                </span>
                ));
              })()}
              {/* 空き尺があれば任意 CM を新規追加 (旧 program_breaks の粒度)。full なら非表示。 */}
              {(() => {
                const opts = d.cm_options.filter((c) => c.grid === b.grid);
                if (b.full || opts.length === 0) return null;
                return (
                  <select value="" disabled={busy} onChange={(e) => addItem(b.break_id, e.target.value)} title={`CM を追加 (${b.grid} 枠・残 ${b.remaining})`} style={{ fontSize: ".72rem", padding: ".05rem .2rem", maxWidth: "8rem", color: "var(--accent)", background: "transparent", border: "1px dashed var(--line)", borderRadius: 3, cursor: "pointer" }}>
                    <option value="">＋CM (残{b.remaining})</option>
                    {opts.map((c) => <option key={c.id} value={String(c.id)}>{c.name}</option>)}
                  </select>
                );
              })()}
            </div>
          ))}
        </section>
      ))}
      <p className="muted" style={{ fontSize: ".75rem" }}>↻ で未送出枠の CM を入替 (送出済は不可)。線引きエディタ (band) は「旧画面で線引き」へ。自動補充は契約/考査変更の反映 (未送出枠のみ)。</p>
    </StudioPage>
  );
}
