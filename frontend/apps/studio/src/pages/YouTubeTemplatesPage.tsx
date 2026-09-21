// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** YouTube broadcast preset 用テンプレート管理 (/youtube/templates)。
 *
 * title_template + description_template のセットを事前登録し、
 * シリーズ編集の preset 作成フォームから「テンプレートから読み込む」で流し込む。
 *
 * プレースホルダ (番組専用枠 preset 用):
 *   {program}  番組タイトル
 *   {channel}  チャンネル名
 *   {date}     放送日 (JST, YYYY-MM-DD)
 *   {start}    開始時刻 (JST, HH:MM)
 *   {end}      終了時刻 (JST, HH:MM)
 */

import { useEffect, useState } from "react";

import { Notice, StudioPage } from "../atoms";

import { postJson, readCookie } from "../hooks";

type Tmpl = {
  id: number;
  name: string;
  title_template: string;
  description_template: string;
};

const _EMPTY = { name: "", title_template: "{program} | {channel}", description_template: "" };
const _URL = "/api/v1/admin/youtube/description-templates";

const PLACEHOLDERS = [
  { ph: "{program}", desc: "番組タイトル" },
  { ph: "{channel}", desc: "チャンネル名" },
  { ph: "{date}", desc: "放送日 (JST, YYYY-MM-DD)" },
  { ph: "{start}", desc: "開始時刻 (JST, HH:MM)" },
  { ph: "{end}", desc: "終了時刻 (JST, HH:MM)" },
];

function TmplForm({
  init,
  onSave,
  onCancel,
  busy,
}: {
  init: typeof _EMPTY;
  onSave: (f: typeof _EMPTY) => Promise<void>;
  onCancel: () => void;
  busy: boolean;
}) {
  const [f, setF] = useState(init);
  const set = <K extends keyof typeof _EMPTY>(k: K, v: (typeof _EMPTY)[K]) =>
    setF((p) => ({ ...p, [k]: v }));

  return (
    <div className="card" style={{ marginBottom: ".8rem" }}
      onKeyDown={(e) => { if (e.key === "Enter" && (e.target as HTMLElement).tagName !== "TEXTAREA") e.preventDefault(); }}>
      <label className="field" style={{ display: "block", marginBottom: ".4rem" }}>
        <span>テンプレート名 *</span>
        <input value={f.name} onChange={(e) => set("name", e.target.value)} required
          placeholder="レギュラー番組A" style={{ width: "20rem" }} />
      </label>
      <label className="field" style={{ display: "block", marginBottom: ".4rem" }}>
        <span>タイトルテンプレート</span>
        <input value={f.title_template} onChange={(e) => set("title_template", e.target.value)}
          placeholder="{program} | {channel}" style={{ width: "100%", maxWidth: "36rem" }} />
      </label>
      <label className="field" style={{ display: "block", marginBottom: ".6rem" }}>
        <span>説明テンプレート</span>
        <textarea value={f.description_template} onChange={(e) => set("description_template", e.target.value)}
          rows={5} style={{ width: "100%", maxWidth: "36rem", fontFamily: "monospace", fontSize: ".83rem" }} />
      </label>
      <div style={{ display: "flex", gap: ".5rem" }}>
        <button className="btn" type="button" disabled={busy || !f.name.trim()}
          onClick={() => void onSave(f)}>保存</button>
        <button className="btn secondary" type="button" onClick={onCancel}>キャンセル</button>
      </div>
    </div>
  );
}

export function YouTubeTemplatesPage() {
  const [items, setItems] = useState<Tmpl[]>([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Tmpl | null>(null);

  async function load() {
    setErr("");
    const res = await fetch(_URL, { credentials: "same-origin" });
    if (!res.ok) { setErr("読み込み失敗 (staff 権限が必要)"); return; }
    setItems(await res.json());
  }

  useEffect(() => { void load(); }, []);

  async function save(f: typeof _EMPTY, id?: number) {
    setBusy(true); setErr(""); setMsg("");
    const url = id ? `${_URL}/${id}` : _URL;
    const { status, data } = await postJson(url, f);
    setBusy(false);
    if (status === 200) {
      setMsg(id ? "更新しました" : "作成しました");
      setCreating(false); setEditing(null);
      const updated = data as Tmpl;
      setItems((prev) => id ? prev.map((t) => t.id === id ? updated : t) : [...prev, updated]);
    } else setErr("保存に失敗しました");
  }

  async function del(t: Tmpl) {
    if (!window.confirm(`「${t.name}」を削除しますか？`)) return;
    setBusy(true); setErr(""); setMsg("");
    const res = await fetch(`${_URL}/${t.id}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRFToken": readCookie("csrftoken") },
    });
    setBusy(false);
    if (res.ok) { setMsg("削除しました"); setItems((prev) => prev.filter((x) => x.id !== t.id)); }
    else setErr("削除に失敗しました");
  }

  return (
    <StudioPage title="YouTube テンプレート管理"
      actions={
        !creating && !editing
          ? <button className="btn" type="button" onClick={() => setCreating(true)}>＋ テンプレートを追加</button>
          : undefined
      }>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}

      {/* プレースホルダ一覧 */}
      <div className="card" style={{ marginBottom: "1rem", background: "#151a1f" }}>
        <p style={{ margin: "0 0 .4rem", fontSize: ".8rem", fontWeight: 600, color: "var(--muted)" }}>
          利用可能なプレースホルダ（番組専用枠 preset）
        </p>
        <div style={{ display: "flex", gap: ".4rem 1.4rem", flexWrap: "wrap" }}>
          {PLACEHOLDERS.map(({ ph, desc }) => (
            <span key={ph} style={{ fontSize: ".8rem" }}>
              <code style={{ background: "#2a3040", padding: "0 .3rem", borderRadius: 3 }}>{ph}</code>
              <span className="muted" style={{ marginLeft: ".3rem" }}>{desc}</span>
            </span>
          ))}
        </div>
      </div>

      {creating && (
        <TmplForm init={_EMPTY} onSave={(f) => save(f)} onCancel={() => setCreating(false)} busy={busy} />
      )}

      {items.length === 0 && !creating && (
        <p className="muted">テンプレートがありません。「＋ テンプレートを追加」で作成してください。</p>
      )}

      {items.map((t) => (
        <div key={t.id}>
          {editing?.id === t.id ? (
            <TmplForm init={{ name: t.name, title_template: t.title_template, description_template: t.description_template }}
              onSave={(f) => save(f, t.id)} onCancel={() => setEditing(null)} busy={busy} />
          ) : (
            <section className="card" style={{ marginBottom: ".6rem" }}>
              <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline", flexWrap: "wrap" }}>
                <strong style={{ fontSize: ".95rem" }}>{t.name}</strong>
                <span style={{ marginLeft: "auto", display: "flex", gap: ".5rem" }}>
                  <button className="btn secondary" type="button" style={{ fontSize: ".78rem", padding: ".1rem .5rem" }}
                    onClick={() => { setEditing(t); setCreating(false); }}>編集</button>
                  <button className="btn danger" type="button" disabled={busy} style={{ fontSize: ".78rem", padding: ".1rem .5rem" }}
                    onClick={() => void del(t)}>削除</button>
                </span>
              </div>
              <div style={{ marginTop: ".4rem", fontSize: ".82rem" }}>
                <div style={{ marginBottom: ".2rem" }}>
                  <span className="muted">タイトル: </span>
                  <code style={{ background: "#1e2228", padding: "0 .4rem", borderRadius: 3 }}>{t.title_template || "—"}</code>
                </div>
                {t.description_template && (
                  <pre style={{ margin: "0", fontSize: ".78rem", color: "var(--muted)", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: "6em", overflow: "hidden", background: "#1e2228", padding: ".3rem .5rem", borderRadius: 3 }}>
                    {t.description_template}
                  </pre>
                )}
              </div>
            </section>
          )}
        </div>
      ))}
    </StudioPage>
  );
}
