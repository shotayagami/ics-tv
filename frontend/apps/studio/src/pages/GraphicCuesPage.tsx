// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Notice, OldScreenLink, StudioPage } from "../atoms";

import type { GraphicCuesOut } from "../hooks";
import { postFile, postForm } from "../hooks";
import { RouterLink } from "../links";

type CueKind = "text" | "graphic" | "video";

/** 自動グラフィック graphic_cues (#Phase2e-1)。owner(series/program/filler) の CG キュー一覧 +
 * キュー追加(文字 / 画像・動画+文字 / 動画クリップ)・削除。画像/動画アップロードは
 * 既存 graphic_cue_add へ multipart POST する (旧画面と同一の _build_data を共有)。 */
export function GraphicCuesPage() {
  const { slug = "", owner = "", ownerId = "" } = useParams();
  const [d, setD] = useState<GraphicCuesOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<CueKind>("text");
  const [advanced, setAdvanced] = useState(false);

  const load = useCallback(() => {
    if (!slug || !owner || !ownerId) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/graphic-cues/{owner}/{owner_id}", { params: { path: { slug, owner, owner_id: Number(ownerId) } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug, owner, ownerId]);
  useEffect(load, [load]);

  const base = `/scheduling/ch/${slug}/graphic-cues/${owner}/${ownerId}`;

  async function op(url: string, fields: Record<string, string>, ok: string, confirmMsg?: string) {
    if (busy) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    setMsg("");
    setErr("");
    const { status } = await postForm(url, fields);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg(ok);
      load();
    } else setErr("操作に失敗しました");
  }

  async function addCue(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    const form = e.currentTarget;
    const f = new FormData(form);
    const str = (k: string) => String(f.get(k) || "").trim();

    // 送信フィールドを種別に応じて構築 (空値は送らない = Django _build_data の分岐に合わせる)。
    const fields: Record<string, string | Blob> = {
      layer: str("layer") || "30",
      kind,
      show_at_s: str("show_at_s") || "0",
    };
    const hide = str("hide_at_s");
    if (hide) fields.hide_at_s = hide;
    const seq = str("seq");
    if (seq) fields.seq = seq;
    const template = str("template");
    if (template) fields.template = template;

    const raw = str("data");
    if (advanced && raw) {
      // 生 JSON は最優先 (複数要素の上級用)。他フィールドは Django 側で無視される。
      fields.data = raw;
    } else if (kind === "graphic") {
      const caption = str("text");
      if (caption) fields.text = caption;
      const file = f.get("image");
      if (file instanceof File && file.size > 0) fields.image = file;
      else {
        const url = str("image_url");
        if (url) fields.image_url = url;
      }
      fields.media = str("media") || "image";
      if (f.get("loop")) fields.loop = "on";
      for (const k of ["x", "y", "w", "size"]) {
        const v = str(k);
        if (v) fields[k] = v;
      }
      const color = str("color");
      if (color) fields.color = color;
      const align = str("align");
      if (align) fields.align = align;
    } else if (kind === "video") {
      const clip = str("clip");
      if (clip) fields.clip = clip;
      if (f.get("loop")) fields.loop = "on";
    } else {
      const text = str("text");
      if (text) fields.text = text;
    }

    setBusy(true);
    setMsg("");
    setErr("");
    const { status } = await postFile(`${base}/add/`, fields);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg("キューを追加しました");
      form.reset();
      setAdvanced(false);
      load();
    } else setErr("追加に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  const parent =
    owner === "series"
      ? { label: "週間編成", href: `/series/${slug}` }
      : owner === "filler"
        ? { label: "チャンネル設定", href: `/channels/${slug}/settings` }
        : { label: "番組編集", href: `/program-form/${slug}?program_id=${ownerId}` };

  return (
    <StudioPage
      title="自動グラフィック"
      breadcrumb={<Breadcrumb items={[parent]} linkComponent={RouterLink} />}
      actions={
        <>
          <span className="muted">{d.title}{d.subtitle && ` · ${d.subtitle}`}</span>
          <OldScreenLink href={d.back_url}>旧画面</OldScreenLink>
        </>
      }
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <div className="card" style={{ marginBottom: "1rem" }}>
        <table>
          <thead><tr><th>L</th><th>種別</th><th>内容</th><th>表示</th><th>終了</th><th></th></tr></thead>
          <tbody>
            {d.cues.map((c) => (
              <tr key={c.id}>
                <td className="muted">L{c.layer}</td>
                <td>{c.kind}</td>
                <td>{c.summary}</td>
                <td style={{ whiteSpace: "nowrap" }}>{c.show_s}s</td>
                <td style={{ whiteSpace: "nowrap" }}>{c.hide_s != null ? `${c.hide_s}s` : <span className="muted">末尾</span>}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn" type="button" disabled={busy} onClick={() => op(`${base}/cue/${c.id}/delete/`, {}, "削除しました", "このキューを削除しますか？")} style={{ fontSize: ".72rem", padding: "0 .4rem", background: "var(--warn)" }}>×</button>
                </td>
              </tr>
            ))}
            {d.cues.length === 0 && <tr><td colSpan={6} className="muted">キューがありません。</td></tr>}
          </tbody>
        </table>
      </div>

      <form onSubmit={addCue} className="card">
        <h3 style={{ margin: "0 0 .4rem" }}>キューを追加</h3>
        {/* 共通: レイヤ / 種別 / 表示・終了 / seq / テンプレ */}
        <div style={{ display: "flex", gap: ".4rem", alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="field"><span>レイヤ</span><input type="number" name="layer" min={1} max={89} defaultValue={30} style={{ width: "4rem" }} /></label>
          <label className="field"><span>種別</span>
            <select name="kind" value={kind} onChange={(e) => setKind(e.target.value as CueKind)}>
              <option value="text">文字</option>
              <option value="graphic">画像/動画+文字</option>
              <option value="video">動画クリップ</option>
            </select>
          </label>
          <label className="field"><span>表示(秒)</span><input type="number" name="show_at_s" min={0} defaultValue={0} style={{ width: "5rem" }} /></label>
          <label className="field"><span>終了(秒,空=末尾)</span><input type="number" name="hide_at_s" min={0} style={{ width: "5rem" }} /></label>
          <label className="field"><span>seq</span><input type="number" name="seq" min={0} defaultValue={0} style={{ width: "3.5rem" }} /></label>
          <label className="field"><span>テンプレ(任意)</span><input name="template" placeholder="freeform 等" style={{ width: "9rem" }} /></label>
        </div>

        {/* 文字キュー */}
        {kind === "text" && !advanced && (
          <div style={{ display: "flex", gap: ".4rem", alignItems: "flex-end", flexWrap: "wrap", marginTop: ".5rem" }}>
            <label className="field" style={{ flex: 1, minWidth: "12rem" }}><span>テキスト</span><input name="text" placeholder="表示する文字" /></label>
          </div>
        )}

        {/* 画像/動画+文字キュー */}
        {kind === "graphic" && !advanced && (
          <>
            <div style={{ display: "flex", gap: ".4rem", alignItems: "flex-end", flexWrap: "wrap", marginTop: ".5rem" }}>
              <label className="field"><span>画像/動画ファイル</span><input type="file" name="image" accept="image/*,video/*" /></label>
              <label className="field" style={{ flex: 1, minWidth: "10rem" }}><span>または URL</span><input name="image_url" placeholder="https://… (ファイル未指定時)" /></label>
              <label className="field"><span>メディア種別</span><select name="media" defaultValue="image"><option value="image">画像</option><option value="video">動画</option></select></label>
              <label className="field" style={{ flexDirection: "row", gap: ".3rem", alignItems: "center" }}><input type="checkbox" name="loop" /><span>ループ(動画)</span></label>
            </div>
            <div style={{ display: "flex", gap: ".4rem", alignItems: "flex-end", flexWrap: "wrap", marginTop: ".5rem" }}>
              <label className="field" style={{ flex: 1, minWidth: "10rem" }}><span>テキスト(任意)</span><input name="text" placeholder="重ねる文字" /></label>
              <label className="field"><span>x</span><input type="number" name="x" style={{ width: "4rem" }} /></label>
              <label className="field"><span>y</span><input type="number" name="y" style={{ width: "4rem" }} /></label>
              <label className="field"><span>w</span><input type="number" name="w" style={{ width: "4rem" }} /></label>
              <label className="field"><span>size</span><input type="number" name="size" style={{ width: "4rem" }} /></label>
              <label className="field"><span>色</span><input name="color" placeholder="#ffffff" style={{ width: "6rem" }} /></label>
              <label className="field"><span>整列</span><select name="align" defaultValue=""><option value="">—</option><option value="left">left</option><option value="center">center</option><option value="right">right</option></select></label>
            </div>
          </>
        )}

        {/* 動画クリップキュー */}
        {kind === "video" && !advanced && (
          <div style={{ display: "flex", gap: ".4rem", alignItems: "flex-end", flexWrap: "wrap", marginTop: ".5rem" }}>
            <label className="field" style={{ flex: 1, minWidth: "12rem" }}><span>クリップ(ファイル名/識別子)</span><input name="clip" placeholder="送出ノード上のクリップ名" /></label>
            <label className="field" style={{ flexDirection: "row", gap: ".3rem", alignItems: "center" }}><input type="checkbox" name="loop" /><span>ループ</span></label>
          </div>
        )}

        {/* 上級: 生 JSON (data を直接指定。複数要素等) */}
        {advanced && (
          <label className="field" style={{ display: "block", marginTop: ".5rem" }}>
            <span>生 JSON data (最優先)</span>
            <textarea name="data" rows={4} placeholder='{"elements":[{"url":"…","media":"image","text":"…"}]}' style={{ width: "100%", fontFamily: "monospace", fontSize: ".8rem" }} />
          </label>
        )}

        <div style={{ display: "flex", gap: ".6rem", alignItems: "center", marginTop: ".6rem" }}>
          <button className="btn" type="submit" disabled={busy}>＋追加</button>
          <label className="field" style={{ flexDirection: "row", gap: ".3rem", alignItems: "center" }}>
            <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} />
            <span className="muted">上級 (生 JSON)</span>
          </label>
        </div>
      </form>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>画像/動画はアップロード (R2 保存) または URL 指定に対応。展開時に Series→Program へコピーされます。より複雑な編集は「旧画面」でも可能です。</p>
    </StudioPage>
  );
}
