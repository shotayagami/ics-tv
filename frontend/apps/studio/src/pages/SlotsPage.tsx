// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { ChannelPicker, EmptyState, Notice, OldScreenLink, StatusBadge, StudioPage, type StatusTone } from "../atoms";

import { RouterLink } from "../links";

import type { SlotListOut, YtRollingConfigIn, YtRollingConfigOut, YtSlotRow } from "../hooks";
import { postForm } from "../hooks";

const SLOT_TONE: Record<string, StatusTone> = {
  live: "ok",
  complete: "neutral",
  error: "danger",
  ready: "ok",
  testing: "warn",
};

/** 1スロット行。タイトル・説明文のインライン編集 + テンプレ適用 + 削除。 */
function SlotRow({ s, onOp, busy }: { s: YtSlotRow; onOp: (path: string, fields: Record<string, string>, ok: string, confirmMsg?: string) => void; busy: boolean }) {
  const [title, setTitle] = useState(s.title);
  const [description, setDescription] = useState(s.description);
  const dirty = title !== s.title || description !== s.description;
  return (
    <tr>
      <td style={{ whiteSpace: "nowrap" }}>{s.window}</td>
      <td><StatusBadge label={s.status} tone={SLOT_TONE[s.status] ?? "neutral"} />{s.manual && <span className="muted" style={{ fontSize: ".7rem" }}> ·手動</span>}</td>
      <td className="muted" style={{ fontSize: ".75rem", whiteSpace: "nowrap" }}>{s.broadcast_id || "—"}</td>
      <td>
        <input value={title} onChange={(e) => setTitle(e.target.value)} style={{ width: "14rem", display: "block", marginBottom: ".2rem" }} />
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} style={{ width: "14rem", fontSize: ".78rem", fontFamily: "monospace", resize: "vertical" }} placeholder="説明文 (省略可)" />
      </td>
      <td style={{ whiteSpace: "nowrap", fontSize: ".78rem" }}>
        <button className="btn" type="button" disabled={busy || !dirty} onClick={() => onOp(`/admin-ui/slot/${s.id}/meta/`, { title, description }, "メタを更新しました")} style={{ fontSize: ".74rem", padding: ".1rem .4rem", opacity: dirty ? 1 : 0.5 }}>保存</button>
        <button className="btn" type="button" disabled={busy} onClick={() => onOp(`/admin-ui/slot/${s.id}/apply-template/`, {}, "テンプレートを適用しました")} style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginLeft: ".3rem" }}>テンプレ適用</button>
        <button className="btn" type="button" disabled={busy} onClick={() => onOp(`/admin-ui/slot/${s.id}/delete/`, {}, "スロットを削除しました", "このスロットを削除しますか？")} style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginLeft: ".3rem", background: "var(--warn)" }}>削除</button>
      </td>
      {s.error && <td style={{ color: "var(--warn)", fontSize: ".75rem" }} title={s.error}>⚠</td>}
    </tr>
  );
}

/** rolling 枠生成テンプレ (YoutubeConfig) の閲覧・編集。旧 Django admin のみだった per-channel 設定を SPA 化。
 * 保存は upsert、既存枠への反映は resync を明示的に叩く (resync_youtube_slot_meta 相当)。 */
function RollingConfigCard({ slug }: { slug: string }) {
  const [cfg, setCfg] = useState<YtRollingConfigOut | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [noteErr, setNoteErr] = useState(false);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/youtube/{slug}/config", { params: { path: { slug } } })
      .then(({ data }) => data && setCfg(data))
      .catch(() => {});
  }, [slug]);
  useEffect(load, [load]);

  function set<K extends keyof YtRollingConfigOut>(k: K, v: YtRollingConfigOut[K]) {
    setCfg((prev) => (prev ? { ...prev, [k]: v } : prev));
  }

  async function save() {
    if (!cfg || busy) return;
    setBusy(true);
    setNote("");
    const body: YtRollingConfigIn = {
      title_template: cfg.title_template,
      description_template: cfg.description_template,
      privacy: cfg.privacy,
      enable_monitor: cfg.enable_monitor,
      rolling_hours: cfg.rolling_hours,
      slot_minutes: cfg.slot_minutes,
      nudge_lead_minutes: cfg.nudge_lead_minutes,
      nudge_template: cfg.nudge_template,
      nudge_ended_template: cfg.nudge_ended_template,
    };
    const { data, error } = await api.POST("/api/v1/admin/youtube/{slug}/config", { params: { path: { slug } }, body });
    setBusy(false);
    if (error) {
      setNoteErr(true);
      setNote((error as { detail?: string })?.detail || "保存に失敗しました");
      return;
    }
    if (data) setCfg(data);
    setNoteErr(false);
    setNote("保存しました。既存枠へ反映するには「既存枠へ反映」を実行してください。");
  }

  async function resync() {
    if (busy) return;
    if (!window.confirm("現在のテンプレートを未終了の既存枠へ再生成・反映します。YouTube 側のタイトル/説明も更新されます。実行しますか？")) return;
    setBusy(true);
    setNote("");
    const { data, error } = await api.POST("/api/v1/admin/youtube/{slug}/config/resync", { params: { path: { slug } } });
    setBusy(false);
    if (error) {
      setNoteErr(true);
      setNote((error as { detail?: string })?.detail || "反映に失敗しました");
      return;
    }
    setNoteErr(false);
    setNote(`既存枠へ反映しました (${JSON.stringify(data?.stats ?? {})})`);
  }

  if (!cfg) return null;

  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: ".6rem" }}>
        <button className="btn ghost" type="button" onClick={() => setOpen((o) => !o)} style={{ fontSize: ".8rem" }}>
          {open ? "▾" : "▸"} 枠生成設定 (rolling)
        </button>
        <span className="muted" style={{ fontSize: ".78rem" }}>
          {cfg.rolling_hours}h 先まで · 1枠 {cfg.slot_minutes}分 · {cfg.privacy}
          {!cfg.exists && " · ⚠ 未設定 (既定値)"}
        </span>
      </div>

      {open && (
        <div style={{ marginTop: ".7rem" }}>
          {note && <Notice variant={noteErr ? "error" : "success"}>{note}</Notice>}
          <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap", marginBottom: ".5rem" }}>
            <label className="field"><span>rolling 時間 (h)</span><input type="number" min={1} value={cfg.rolling_hours} onChange={(e) => set("rolling_hours", Number(e.target.value))} style={{ width: "5rem" }} /></label>
            <label className="field"><span>1枠の長さ (分)</span><input type="number" min={1} value={cfg.slot_minutes} onChange={(e) => set("slot_minutes", Number(e.target.value))} style={{ width: "5rem" }} /></label>
            <label className="field"><span>公開範囲</span>
              <select value={cfg.privacy} onChange={(e) => set("privacy", e.target.value)}>
                {cfg.privacy_choices.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </label>
            <label className="field" style={{ flexDirection: "row", gap: ".3rem", alignItems: "center", alignSelf: "flex-end" }}>
              <input type="checkbox" checked={cfg.enable_monitor} onChange={(e) => set("enable_monitor", e.target.checked)} /><span>モニター有効 (testing 経由)</span>
            </label>
            <label className="field"><span>次枠誘導 lead (分・0=無効)</span><input type="number" min={0} value={cfg.nudge_lead_minutes} onChange={(e) => set("nudge_lead_minutes", Number(e.target.value))} style={{ width: "5rem" }} /></label>
          </div>
          <label className="field" style={{ display: "block", marginBottom: ".5rem" }}>
            <span>タイトルテンプレ <span className="muted">{"{channel}/{date}/{start}/{end}"}</span></span>
            <input value={cfg.title_template} onChange={(e) => set("title_template", e.target.value)} style={{ width: "100%" }} />
          </label>
          <label className="field" style={{ display: "block", marginBottom: ".5rem" }}>
            <span>説明テンプレ <span className="muted">{"{channel}/{date}/{start}/{end}/{programs}"}</span></span>
            <textarea value={cfg.description_template} onChange={(e) => set("description_template", e.target.value)} rows={3} style={{ width: "100%", fontFamily: "monospace", fontSize: ".8rem" }} />
          </label>
          <label className="field" style={{ display: "block", marginBottom: ".5rem" }}>
            <span>誘導文 (予告中) <span className="muted">{"{url}/{start}/{end}"}</span></span>
            <textarea value={cfg.nudge_template} onChange={(e) => set("nudge_template", e.target.value)} rows={2} style={{ width: "100%", fontFamily: "monospace", fontSize: ".8rem" }} />
          </label>
          <label className="field" style={{ display: "block", marginBottom: ".6rem" }}>
            <span>誘導文 (終了後) <span className="muted">空=差替なし</span></span>
            <textarea value={cfg.nudge_ended_template} onChange={(e) => set("nudge_ended_template", e.target.value)} rows={2} style={{ width: "100%", fontFamily: "monospace", fontSize: ".8rem" }} />
          </label>
          <div style={{ display: "flex", gap: ".5rem" }}>
            <button className="btn" type="button" disabled={busy} onClick={save}>保存</button>
            <button className="btn ghost" type="button" disabled={busy || !cfg.exists} onClick={resync}>既存枠へ反映</button>
          </div>
          <p className="muted" style={{ fontSize: ".72rem", marginTop: ".5rem" }}>保存はテンプレを更新するのみ。generate_slots が新規枠に使います。既存の未終了枠へ行き渡らせるには「既存枠へ反映」を実行してください。</p>
        </div>
      )}
    </div>
  );
}

/** YouTube スロット dashboard (#Phase2d-12)。2h×12 ローリングのスロットを監視 + メタ更新/テンプレ適用/削除。
 * YouTube OAuth / Cloudflare Live 設定は外部 OAuth フローのため旧 channel settings へ。 */
export function SlotsPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<SlotListOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/youtube/{slug}/slots", { params: { path: { slug } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug]);
  useEffect(load, [load]);

  async function onOp(path: string, fields: Record<string, string>, ok: string, confirmMsg?: string) {
    if (busy) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status, data } = await postForm(path, fields);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg(ok);
      load();
    } else setErr(typeof data === "string" ? data : "操作に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="YouTube スロット"
      channel={
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/slots/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
      }
      actions={<OldScreenLink href={d.settings_url}>チャンネル設定 (OAuth/CF)</OldScreenLink>}
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <RollingConfigCard slug={slug} />

      <div className="card">
        <table>
          <thead><tr><th>枠</th><th>状態</th><th>broadcast</th><th>タイトル</th><th>操作</th><th></th></tr></thead>
          <tbody>
            {d.slots.map((s) => <SlotRow key={s.id} s={s} onOp={onOp} busy={busy} />)}
            {d.slots.length === 0 && <tr><td colSpan={6} className="muted">スロットがありません (チャンネル設定で YouTube/CF 連携が必要)。</td></tr>}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>2h×12 ローリング。YouTube OAuth / Cloudflare Live 設定・配信入力作成は「チャンネル設定」(旧画面) へ。</p>
    </StudioPage>
  );
}
