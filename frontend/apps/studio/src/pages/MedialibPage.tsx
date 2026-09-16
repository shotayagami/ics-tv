// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Notice, StatusBadge, StudioPage, type StatusTone } from "../atoms";

import type { MedialibOut, MlAsset } from "../hooks";
import { postForm } from "../hooks";
import { MediaUploadPanel } from "./MediaUploadPanel";

const ASSET_TONE: Record<string, StatusTone> = { ready: "ok", processing: "neutral", pending: "neutral", failed: "danger" };
function statusBadge(s: string) {
  return <StatusBadge label={s} tone={ASSET_TONE[s] ?? "neutral"} />;
}

function Thumb({ url, size = 32 }: { url: string; size?: number }) {
  return (
    <span style={{ display: "inline-block", width: size, height: (size * 9) / 16, borderRadius: 3, overflow: "hidden", background: "#1d1d1d", border: "1px solid var(--line)" }}>
      {url ? <img src={url} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} /> : null}
    </span>
  );
}

type AssetRowProps = {
  a: MlAsset;
  busy: boolean;
  op: (url: string, fields: Record<string, string>, okMsg: string, confirmMsg?: string) => Promise<void>;
};

function AssetRow({ a, busy, op }: AssetRowProps) {
  return (
    <tr key={a.id}>
      <td><Thumb url={a.thumbnail_url} /></td>
      <td>{a.title}</td>
      <td style={{ whiteSpace: "nowrap" }}>{a.duration_display}</td>
      <td>{statusBadge(a.normalize_status)}</td>
      <td style={{ whiteSpace: "nowrap", fontSize: ".78rem" }}>
        <button className="btn" type="button" disabled={busy} onClick={() => op(`/medialib/asset/${a.id}/renormalize/`, {}, "再正規化キューに投入しました", `「${a.title}」を再正規化しますか？`)} style={{ fontSize: ".76rem", padding: ".1rem .4rem" }}>再正規化</button>
        {a.is_program && <Link to={`/cuesheet/${a.id}`} style={{ marginLeft: ".5rem", color: "#9bd" }}>キューシート{a.has_cuesheet ? " ✓" : ""}</Link>}
        <a href={a.edit_url} className="muted" style={{ marginLeft: ".5rem" }}>編集</a>
      </td>
    </tr>
  );
}

type AssetGroupProps = {
  label: string;
  assets: MlAsset[];
  busy: boolean;
  op: AssetRowProps["op"];
};

function AssetGroup({ label, assets, busy, op }: AssetGroupProps) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginBottom: ".6rem" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{ background: "none", border: "none", color: "var(--fg)", cursor: "pointer", display: "flex", alignItems: "center", gap: ".4rem", padding: ".3rem 0", fontSize: ".85rem", fontWeight: 600 }}
      >
        <span style={{ fontSize: ".7rem", color: "var(--muted)", transform: open ? "rotate(90deg)" : "none", display: "inline-block", transition: "transform .15s" }}>▶</span>
        {label}
        <span className="muted" style={{ fontSize: ".78rem", fontWeight: "normal" }}>{assets.length} 件</span>
      </button>
      {open && (
        <table style={{ marginTop: ".2rem" }}>
          <thead><tr><th></th><th>タイトル</th><th>尺</th><th>正規化</th><th>操作</th></tr></thead>
          <tbody>
            {assets.map((a) => <AssetRow key={a.id} a={a} busy={busy} op={op} />)}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** グループキーの表示順。series_ → ch_ → cm_ → other の順で並べる。 */
function groupSortKey(key: string): string {
  if (key.startsWith("series_")) return `0_${key}`;
  if (key.startsWith("ch_")) return `1_${key}`;
  if (key.startsWith("pl_")) return `2_${key}`;
  if (key.startsWith("cm_")) return `3_${key}`;
  return `4_${key}`;
}

/** 素材ライブラリ medialib ダッシュボード (#Phase2d-6)。素材一覧(フィルタ+再正規化) +
 * CM 在庫/考査 + バンドル/フィラー概観。各エディタは旧画面 (edit_url) へ。 */
export function MedialibPage() {
  const [sp, setSp] = useSearchParams();
  const kind = sp.get("kind") ?? "";
  const status = sp.get("status") ?? "";
  const [d, setD] = useState<MedialibOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/medialib/dashboard", { params: { query: { kind, status } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [kind, status]);
  useEffect(load, [load]);

  function setFilter(key: "kind" | "status", v: string) {
    const next = new URLSearchParams(sp);
    if (v) next.set(key, v);
    else next.delete(key);
    setSp(next);
  }

  async function op(url: string, fields: Record<string, string>, okMsg: string, confirmMsg?: string) {
    if (busy) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status: st, data } = await postForm(url, fields);
    setBusy(false);
    if (st === 200 || st === 204) {
      setMsg(okMsg);
      load();
    } else setErr((data && (data.detail || data.error)) || "操作に失敗しました");
  }

  if (err && !d) return <p className="st-loading">{err}</p>;
  if (!d) return <p className="st-loading">読み込み中…</p>;

  // グループ化
  const groupMap = new Map<string, { label: string; assets: MlAsset[] }>();
  for (const a of d.assets) {
    const key = a.group_key;
    if (!groupMap.has(key)) groupMap.set(key, { label: a.group_label, assets: [] });
    groupMap.get(key)!.assets.push(a);
  }
  const groups = [...groupMap.entries()]
    .sort(([a], [b]) => groupSortKey(a).localeCompare(groupSortKey(b)));

  return (
    <StudioPage title="素材ライブラリ">
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <MediaUploadPanel onCompleted={() => { setMsg("アップロードと検証が完了しました"); load(); }} />

      <section className="card" style={{ marginBottom: "1rem" }}>
        <h2 style={{ display: "flex", gap: ".6rem", alignItems: "center", flexWrap: "wrap" }}>
          素材
          <span className="muted" style={{ fontSize: ".8rem", fontWeight: "normal" }}>{d.assets.length} 件</span>
          <select value={kind} onChange={(e) => setFilter("kind", e.target.value)}>
            <option value="">全種別</option>
            {d.kinds.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
          <select value={status} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">全状態</option>
            {d.statuses.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </h2>
        {groups.length === 0
          ? <p className="muted">該当する素材がありません。</p>
          : groups.map(([key, { label, assets }]) => (
              <AssetGroup key={key} label={label} assets={assets} busy={busy} op={op} />
            ))
        }
      </section>

      <section className="card" style={{ marginBottom: "1rem" }}>
        <h2>CM <span className="muted" style={{ fontSize: ".8rem", fontWeight: "normal" }}>{d.cms.length} 件{d.n_screening_pending ? ` · 未考査 ${d.n_screening_pending}` : ""}</span></h2>
        <table>
          <thead><tr><th>広告主</th><th>grid</th><th>期間</th><th>消化</th><th>在庫</th><th>考査</th><th>操作</th></tr></thead>
          <tbody>
            {d.cms.map((c) => (
              <tr key={c.asset_id}>
                <td>{c.advertiser}</td>
                <td>{c.grid}</td>
                <td style={{ whiteSpace: "nowrap" }}>{c.campaign}</td>
                <td style={{ whiteSpace: "nowrap" }}>{c.aired}</td>
                <td><StatusBadge label={c.stock_label} tone={c.stock_css === "warn" ? "warn" : c.stock_css === "live" ? "ok" : "neutral"} /></td>
                <td style={{ whiteSpace: "nowrap" }}>{c.screening_status === "approved" ? <StatusBadge label="考査OK" tone="ok" /> : c.screening_status === "rejected" ? <StatusBadge label="考査NG" tone="danger" /> : <StatusBadge label="未考査" tone="neutral" />}</td>
                <td style={{ whiteSpace: "nowrap", fontSize: ".78rem" }}>
                  {c.screening_status !== "approved" && <button className="btn" type="button" disabled={busy} onClick={() => op(`/medialib/cm/${c.asset_id}/screening/`, { decision: "approve" }, "考査OKにしました")} style={{ fontSize: ".74rem", padding: ".1rem .4rem" }}>OK</button>}
                  {c.screening_status !== "rejected" && <button className="btn" type="button" disabled={busy} onClick={() => op(`/medialib/cm/${c.asset_id}/screening/`, { decision: "reject" }, "考査NGにしました")} style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginLeft: ".3rem", background: "var(--warn)" }}>NG</button>}
                  <a href={c.edit_url} className="muted" style={{ marginLeft: ".5rem" }}>編集</a>
                </td>
              </tr>
            ))}
            {d.cms.length === 0 && <tr><td colSpan={7} className="muted">CM がありません。</td></tr>}
          </tbody>
        </table>
      </section>

      <div className="st-grid">
        <section className="card">
          <h2>バンドル <span className="muted" style={{ fontSize: ".8rem", fontWeight: "normal" }}>{d.bundles.length}</span></h2>
          <table><tbody>
            {d.bundles.map((b) => <tr key={b.id}><td><a href={b.edit_url} className="muted">{b.name}</a></td><td style={{ textAlign: "right" }}>{b.count}</td></tr>)}
            {d.bundles.length === 0 && <tr><td className="muted">なし</td></tr>}
          </tbody></table>
        </section>
        <section className="card">
          <h2>フィラー <span className="muted" style={{ fontSize: ".8rem", fontWeight: "normal" }}>{d.fillers.length}</span></h2>
          <table><tbody>
            {d.fillers.map((f) => <tr key={f.id}><td><a href={f.edit_url} className="muted">{f.name}</a></td><td style={{ textAlign: "right" }}>{f.count}</td></tr>)}
            {d.fillers.length === 0 && <tr><td className="muted">なし</td></tr>}
          </tbody></table>
        </section>
      </div>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>素材/CM/バンドル/フィラーの各エディタ・キューシートは「編集」リンク (旧画面) へ。</p>
    </StudioPage>
  );
}
