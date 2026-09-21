// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Notice, StudioPage } from "../atoms";

import type { CueSheetOut } from "../hooks";
import { postForm } from "../hooks";
import { RouterLink } from "../links";

/** キューシート エディタ (#Phase2d-10 本丸)。録画素材の内部ランダウン (本編 ⊕ CM枠) を
 * 順序リストで編集。本編/CM枠の追加・並べ替え・削除は既存 cuepoint エンドポイントを postForm 再利用。
 * 素材内オフセット・残尺・検証エラーをサーバ集約で表示。 */
export function CueSheetPage() {
  const { assetId = "" } = useParams();
  const [d, setD] = useState<CueSheetOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!assetId) return;
    api
      .GET("/api/v1/admin/medialib/asset/{asset_id}/cuesheet", { params: { path: { asset_id: Number(assetId) } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [assetId]);
  useEffect(load, [load]);

  async function op(url: string, fields: Record<string, string>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    setMsg("");
    const { status, data } = await postForm(url, fields);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg(okMsg);
      load();
    } else setErr(typeof data === "string" ? data : "操作に失敗しました");
  }

  function addContent(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    op(`/medialib/asset/${assetId}/cuesheet/add/`, { kind: "content", min: String(f.get("min") || "0"), sec: String(f.get("sec") || "0"), ms: "0" }, "本編を追加しました");
  }
  function addBreak(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    op(`/medialib/asset/${assetId}/cuesheet/add/`, { kind: "ad_break", grid: String(f.get("grid") || "15s"), min: String(f.get("min") || "0"), sec: String(f.get("sec") || "0"), ms: "0" }, "CM枠を追加しました");
  }
  function fillRemaining() {
    if (!d || d.remaining_ms <= 0) return;
    const m = Math.floor(d.remaining_ms / 60000);
    const s = Math.floor((d.remaining_ms % 60000) / 1000);
    const ms = d.remaining_ms % 1000;
    op(`/medialib/asset/${assetId}/cuesheet/add/`, { kind: "content", min: String(m), sec: String(s), ms: String(ms) }, "残尺を本編で充填しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="キューシート"
      breadcrumb={
        <Breadcrumb items={[{ label: "素材ライブラリ", href: "/medialib" }]} linkComponent={RouterLink} />
      }
      actions={<span className="muted">{d.asset_title} · 尺 {d.asset_duration}</span>}
    >
      {d.error && <p style={{ color: "var(--warn)", margin: ".2rem 0" }}>⚠ {d.error}</p>}
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <div className="card" style={{ marginBottom: "1rem" }}>
        <table>
          <thead><tr><th>#</th><th>種別</th><th>尺</th><th>素材内オフセット</th><th>grid</th><th>並べ替え/削除</th></tr></thead>
          <tbody>
            {d.points.map((p, i) => (
              <tr key={p.id}>
                <td className="muted">{i + 1}</td>
                <td>{p.kind === "ad_break" ? <span style={{ color: "#d9b15a" }}>● {p.kind_label}</span> : p.kind_label}</td>
                <td style={{ whiteSpace: "nowrap" }}>{p.duration}</td>
                <td style={{ whiteSpace: "nowrap" }}>{p.offset || <span className="muted">—</span>}</td>
                <td>{p.grid}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn" type="button" disabled={busy || i === 0} onClick={() => op(`/medialib/cuepoint/${p.id}/move/`, { direction: "up" }, "並べ替えました")} style={{ fontSize: ".72rem", padding: "0 .4rem" }}>↑</button>
                  <button className="btn" type="button" disabled={busy || i === d.points.length - 1} onClick={() => op(`/medialib/cuepoint/${p.id}/move/`, { direction: "down" }, "並べ替えました")} style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".2rem" }}>↓</button>
                  <button className="btn" type="button" disabled={busy} onClick={() => op(`/medialib/cuepoint/${p.id}/delete/`, {}, "削除しました")} style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".3rem", background: "var(--warn)" }}>×</button>
                </td>
              </tr>
            ))}
            {d.points.length === 0 && <tr><td colSpan={6} className="muted">行がありません。下で本編/CM枠を追加します。</td></tr>}
          </tbody>
        </table>
        <p className="muted" style={{ fontSize: ".8rem", margin: ".4rem 0 0" }}>
          本編合計 {d.content_total}
          {d.remaining_ms > 0 && <> · 残尺 <span style={{ color: "var(--warn)" }}>{d.remaining_disp}</span> <button className="btn" type="button" disabled={busy} onClick={fillRemaining} style={{ fontSize: ".74rem", padding: ".05rem .4rem" }}>残尺を本編で充填</button></>}
        </p>
      </div>

      <div className="st-grid">
        <form onSubmit={addContent} className="card">
          <h3 style={{ margin: "0 0 .4rem" }}>本編を追加</h3>
          <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}>
            <input type="number" name="min" min={0} placeholder="分" style={{ width: "4rem" }} /> 分
            <input type="number" name="sec" min={0} max={59} placeholder="秒" style={{ width: "4rem" }} /> 秒
            <button className="btn" type="submit" disabled={busy}>＋本編</button>
          </span>
        </form>
        <form onSubmit={addBreak} className="card">
          <h3 style={{ margin: "0 0 .4rem" }}>CM枠を追加</h3>
          <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", flexWrap: "wrap" }}>
            <select name="grid" defaultValue="15s"><option value="15s">15秒グリッド</option><option value="20s">20秒グリッド</option></select>
            <input type="number" name="min" min={0} placeholder="分" style={{ width: "4rem" }} /> 分
            <input type="number" name="sec" min={0} max={59} placeholder="秒" style={{ width: "4rem" }} /> 秒
            <button className="btn" type="submit" disabled={busy}>＋CM枠</button>
          </span>
        </form>
      </div>
    </StudioPage>
  );
}
