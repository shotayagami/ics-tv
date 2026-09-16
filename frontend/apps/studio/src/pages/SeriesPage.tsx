// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { ChannelPicker, EmptyState, Notice, StudioPage } from "../atoms";

import { RouterLink } from "../links";

import type { SeriesOut } from "../hooks";
import { postForm } from "../hooks";

/** 週間編成 series (#Phase2d-7)。series + 繰り返しスロットの概観 + 展開/削除。
 * 作成/編集フォーム (サムネ/繰り返し/ソース) は旧画面 (edit_url/new_url) へ。 */
export function SeriesPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<SeriesOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/series", { params: { path: { slug } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug]);
  useEffect(load, [load]);

  async function op(url: string, okMsg: string, confirmMsg?: string) {
    if (busy) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status } = await postForm(url);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg(okMsg);
      load();
    } else setErr("操作に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="週間編成"
      channel={
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/series/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
      }
      actions={
        <>
          <button className="btn" type="button" disabled={busy} onClick={() => op(`/scheduling/ch/${slug}/series/expand/`, "4週先まで展開しました")} title="有効スロットを4週先まで Program へ展開">展開 (4週)</button>
          <Link className="btn" to={`/series/${slug}/new`}>＋シリーズ</Link>
        </>
      }
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      {d.series.length === 0 && <p className="muted">シリーズがありません。「＋シリーズ」で作成します。</p>}
      {d.series.map((s) => (
        <section key={s.id} className="card" style={{ marginBottom: ".8rem" }}>
          <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline", flexWrap: "wrap" }}>
            <h2 style={{ margin: 0 }}>{s.title}</h2>
            {s.genre && <span className="chip" style={{ fontSize: ".75rem", color: "var(--muted)" }}>{s.genre}</span>}
            {!s.is_active && <span style={{ color: "var(--warn)", fontSize: ".78rem" }}>停止中</span>}
            <span className="muted" style={{ fontSize: ".78rem" }}>{s.n_slots} スロット</span>
            <span style={{ marginLeft: "auto", display: "flex", gap: ".5rem", fontSize: ".8rem", flexWrap: "wrap", justifyContent: "flex-end" }}>
              <Link to={`/series/${slug}/edit/${s.id}`} className="muted">編集</Link>
              <Link to={`/graphic-cues/${slug}/series/${s.id}`} className="muted">CG</Link>
              <Link to={`/series/${slug}/edit/${s.id}?tab=forms`} className="muted">フォーム</Link>
              {s.slug && (
                <a href={`/series/${s.slug}/`} target="_blank" rel="noopener" className="muted">番組ページ ↗</a>
              )}
              <button className="btn" type="button" disabled={busy} onClick={() => op(`/scheduling/ch/${slug}/series/${s.id}/delete/`, "削除しました", `シリーズ「${s.title}」を削除しますか？`)} style={{ fontSize: ".76rem", padding: ".1rem .5rem", background: "var(--warn)" }}>削除</button>
            </span>
          </div>
          {s.slots.length > 0 && (
            <table style={{ marginTop: ".4rem" }}>
              <thead><tr><th>繰り返し</th><th>時刻</th><th>尺</th><th>種別</th><th>ソース</th><th></th></tr></thead>
              <tbody>
                {s.slots.map((sl) => (
                  <tr key={sl.id}>
                    <td>{sl.recurrence}</td>
                    <td style={{ whiteSpace: "nowrap" }}>{sl.start_time}</td>
                    <td style={{ whiteSpace: "nowrap" }}>{sl.duration}</td>
                    <td>{sl.program_type === "live" ? "◆生" : "◇録画"}</td>
                    <td>{sl.source}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      {sl.program_type === "live" && (
                        <Link to={`/rundown-template/${slug}/${sl.id}`} className="btn" style={{ fontSize: ".72rem", padding: "0 .4rem", marginRight: ".3rem" }}>定番進行表</Link>
                      )}
                      <button className="btn" type="button" disabled={busy} onClick={() => op(`/scheduling/ch/${slug}/slots/${sl.id}/delete/`, "スロットを削除しました", "このスロットを削除しますか？")} style={{ fontSize: ".72rem", padding: "0 .4rem", background: "var(--warn)" }}>×</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
      <p className="muted" style={{ fontSize: ".75rem" }}>シリーズの作成・編集・スロット・投書フォーム・回は「編集」「＋シリーズ」から。展開 (4週) は beat も週次実行。</p>
    </StudioPage>
  );
}
