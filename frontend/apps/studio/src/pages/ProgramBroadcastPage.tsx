// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 番組専用枠ダッシュボード (/youtube/dedicated/:slug)。
 *
 * series.youtube_dedicated=True の番組に対して ProgramBroadcast を作成・管理する。
 * 旧 /admin-ui/ch/<slug>/youtube/dedicated/ の移植。
 *
 * セクション A: 作成候補 (プリセット設定済み・枠未作成の今後の番組)
 * セクション B: 既存 ProgramBroadcast (状態遷移・削除・チェックリスト)
 */

import { Fragment, useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { ChannelPicker, EmptyState, Notice, OldScreenLink, StatusBadge, StudioPage, type StatusTone } from "../atoms";
import { RouterLink } from "../links";
import { postJson, readCookie } from "../hooks";

const BASE = "/api/v1/admin/youtube";

type SchedChannel = { slug: string; name: string };

type Candidate = {
  program_id: number;
  title: string;
  start_at: string;
  preset_id: number;
  preset_name: string;
};

type ChecklistItem = { key: string; label: string; checked: boolean };

type BroadcastRow = {
  id: number;
  program_id: number;
  program_title: string;
  start_at: string;
  end_at: string;
  status: string;
  manual: boolean;
  broadcast_id: string;
  watch_url: string;
  preset_name: string;
  checklist: ChecklistItem[];
};

type Dashboard = {
  channel: SchedChannel;
  channels: SchedChannel[];
  candidates: Candidate[];
  broadcasts: BroadcastRow[];
  settings_url: string;
};

const STATUS_TONE: Record<string, StatusTone> = {
  live: "ok",
  complete: "neutral",
  error: "danger",
  ready: "ok",
  testing: "warn",
  created: "neutral",
};

/** チェックリスト展開パネル。 */
function ChecklistPanel({
  pb,
  onSave,
  busy,
}: {
  pb: BroadcastRow;
  onSave: (pbId: number, state: Record<string, boolean>) => Promise<void>;
  busy: boolean;
}) {
  const [state, setState] = useState<Record<string, boolean>>(
    Object.fromEntries(pb.checklist.map((c) => [c.key, c.checked])),
  );
  const dirty = pb.checklist.some((c) => state[c.key] !== c.checked);

  return (
    <div style={{ padding: ".5rem .8rem .6rem", borderTop: "1px solid var(--line)", background: "#0e1216" }}>
      <p style={{ margin: "0 0 .4rem", fontSize: ".78rem", color: "var(--muted)", fontWeight: 600 }}>手動チェックリスト</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: ".2rem .8rem", marginBottom: ".5rem" }}>
        {pb.checklist.map((item) => (
          <label key={item.key} style={{ display: "flex", alignItems: "center", gap: ".3rem", fontSize: ".8rem", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={state[item.key] ?? item.checked}
              onChange={(e) => setState((prev) => ({ ...prev, [item.key]: e.target.checked }))}
            />
            {item.label}
          </label>
        ))}
      </div>
      {pb.checklist.length === 0 && (
        <p className="muted" style={{ fontSize: ".78rem", margin: 0 }}>プリセットにチェックリストが定義されていません。</p>
      )}
      {pb.checklist.length > 0 && (
        <button className="btn" type="button" disabled={busy || !dirty}
          style={{ fontSize: ".76rem", padding: ".15rem .6rem", opacity: dirty ? 1 : 0.5 }}
          onClick={() => void onSave(pb.id, state)}>
          チェックリストを保存
        </button>
      )}
    </div>
  );
}

export function ProgramBroadcastPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<Dashboard | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const load = useCallback(() => {
    if (!slug) return;
    fetch(`${BASE}/${slug}/dedicated`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data: Dashboard) => { setD(data); setErr(""); })
      .catch(() => setErr("読み込み失敗 (staff 権限が必要)"));
  }, [slug]);

  useEffect(load, [load]);

  function flash(ok: boolean, okMsg: string, errMsg: string) {
    if (ok) { setMsg(okMsg); setErr(""); }
    else { setErr(errMsg); setMsg(""); }
  }

  async function createBroadcast(programId: number) {
    if (busy) return;
    setBusy(true);
    const { status } = await postJson(`${BASE}/${slug}/dedicated/${programId}/create`, {});
    setBusy(false);
    flash(status === 200, "専用枠を作成しました", "作成に失敗しました");
    if (status === 200) load();
  }

  async function transition(pbId: number, action: string) {
    if (busy) return;
    setBusy(true);
    const { status } = await postJson(`${BASE}/program-broadcast/${pbId}/transition`, { action });
    setBusy(false);
    flash(status === 200, `${action === "go_live" ? "ライブ開始" : "配信終了"}しました`, "遷移に失敗しました");
    if (status === 200) load();
  }

  async function saveChecklist(pbId: number, state: Record<string, boolean>) {
    if (busy) return;
    setBusy(true);
    const { status } = await postJson(`${BASE}/program-broadcast/${pbId}/checklist`, { state });
    setBusy(false);
    flash(status === 200, "チェックリストを保存しました", "保存に失敗しました");
    if (status === 200) load();
  }

  async function del(pb: BroadcastRow) {
    if (!window.confirm(`「${pb.program_title}」の専用枠を削除しますか？`)) return;
    if (busy) return;
    setBusy(true);
    const res = await fetch(`${BASE}/program-broadcast/${pb.id}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRFToken": readCookie("csrftoken") },
    });
    setBusy(false);
    flash(res.ok, "削除しました", "削除に失敗しました");
    if (res.ok) load();
  }

  function toggleExpand(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="番組専用枠"
      channel={
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/youtube/dedicated/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
      }
      actions={<OldScreenLink href={d.settings_url}>チャンネル設定 (OAuth/CF)</OldScreenLink>}
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      {/* セクション A: 作成候補 */}
      <h3 style={{ margin: "0 0 .5rem", fontSize: ".9rem" }}>作成候補</h3>
      <p className="muted" style={{ fontSize: ".78rem", margin: "0 0 .6rem" }}>
        <code>youtube_dedicated=True</code> かつプリセット設定済みのシリーズに含まれる今後の番組で、まだ専用枠がないもの。
      </p>
      <div className="card" style={{ marginBottom: "1.2rem" }}>
        {d.candidates.length === 0 ? (
          <p className="muted" style={{ fontSize: ".82rem", margin: 0 }}>
            作成候補がありません。シリーズ設定で「YouTube 専用枠」を有効化しプリセットを選択してください。
          </p>
        ) : (
          <table>
            <thead>
              <tr><th>番組</th><th>開始</th><th>プリセット</th><th></th></tr>
            </thead>
            <tbody>
              {d.candidates.map((c) => (
                <tr key={c.program_id}>
                  <td style={{ fontSize: ".85rem" }}>{c.title}</td>
                  <td style={{ fontSize: ".82rem", whiteSpace: "nowrap" }} className="muted">{c.start_at}</td>
                  <td style={{ fontSize: ".82rem" }} className="muted">{c.preset_name}</td>
                  <td>
                    <button className="btn" type="button" disabled={busy}
                      style={{ fontSize: ".76rem", padding: ".15rem .6rem" }}
                      onClick={() => void createBroadcast(c.program_id)}>
                      今すぐ作成
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* セクション B: 既存 ProgramBroadcast */}
      <h3 style={{ margin: "0 0 .5rem", fontSize: ".9rem" }}>専用枠一覧 (直近 50 件)</h3>
      <div className="card">
        {d.broadcasts.length === 0 ? (
          <p className="muted" style={{ fontSize: ".82rem", margin: 0 }}>専用枠がありません。</p>
        ) : (
          <table style={{ width: "100%" }}>
            <thead>
              <tr>
                <th>番組</th>
                <th>開始</th>
                <th>状態</th>
                <th>broadcast</th>
                <th>プリセット</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {d.broadcasts.map((pb) => (
                <Fragment key={pb.id}>
                  <tr>
                    <td style={{ fontSize: ".85rem" }}>
                      {pb.program_title}
                      {pb.manual && <span className="muted" style={{ fontSize: ".7rem" }}> ·手動</span>}
                    </td>
                    <td style={{ fontSize: ".8rem", whiteSpace: "nowrap" }} className="muted">{pb.start_at}</td>
                    <td><StatusBadge label={pb.status} tone={STATUS_TONE[pb.status] ?? "neutral"} /></td>
                    <td style={{ fontSize: ".75rem" }} className="muted">
                      {pb.broadcast_id
                        ? <a href={pb.watch_url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>{pb.broadcast_id.slice(0, 11)}…</a>
                        : "—"}
                    </td>
                    <td style={{ fontSize: ".8rem" }} className="muted">{pb.preset_name}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {(pb.status === "ready" || pb.status === "testing") && (
                        <button className="btn" type="button" disabled={busy}
                          style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginRight: ".3rem" }}
                          onClick={() => void transition(pb.id, "go_live")}>Go Live</button>
                      )}
                      {pb.status === "live" && (
                        <button className="btn" type="button" disabled={busy}
                          style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginRight: ".3rem", background: "var(--warn)" }}
                          onClick={() => void transition(pb.id, "complete")}>終了</button>
                      )}
                      <button className="btn secondary" type="button" disabled={busy}
                        style={{ fontSize: ".74rem", padding: ".1rem .4rem", marginRight: ".3rem" }}
                        onClick={() => toggleExpand(pb.id)}>
                        {expanded.has(pb.id) ? "▲" : "▼"} チェック
                      </button>
                      <button className="btn danger" type="button" disabled={busy}
                        style={{ fontSize: ".74rem", padding: ".1rem .4rem" }}
                        onClick={() => void del(pb)}>削除</button>
                    </td>
                  </tr>
                  {expanded.has(pb.id) && (
                    <tr>
                      <td colSpan={6} style={{ padding: 0 }}>
                        <ChecklistPanel pb={pb} onSave={saveChecklist} busy={busy} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </StudioPage>
  );
}
