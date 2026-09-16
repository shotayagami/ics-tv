// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 配信プリセット管理 (/youtube/presets)。
 *
 * YoutubeBroadcastPreset の全フィールド CRUD。旧 /admin-ui/youtube/presets/ の移植。
 * SeriesDetailPage の「インライン簡易作成」(4フィールド) を補完し、latency / DVR /
 * チェックリスト定義など YouTube Data API 設定項目を SPAで完結させる。
 */

import { useEffect, useState } from "react";

import { Notice, StudioPage } from "../atoms";
import { postJson, readCookie } from "../hooks";

const BASE = "/api/v1/admin/youtube/presets";

type ChannelOpt = { id: number; slug: string; name: string };
type CatOpt = { value: number | null; label: string };
type ChecklistItem = { key: string; label: string; default: boolean };

type PresetListItem = {
  id: number;
  name: string;
  channel_name: string | null;
  privacy: string;
  latency: string;
  category_id: number | null;
};

type PresetDetail = {
  id: number;
  name: string;
  channel_id: number | null;
  title_template: string;
  description_template: string;
  category_id: number | null;
  tags: string[];
  privacy: string;
  made_for_kids: boolean;
  default_language: string;
  default_audio_language: string;
  latency: string;
  enable_dvr: boolean;
  enable_embed: boolean;
  enable_auto_start: boolean;
  enable_auto_stop: boolean;
  record_from_start: boolean;
  license: string;
  public_stats_viewable: boolean;
  thumbnail_id: number | null;
  playlist_id: string;
  manual_checklist: ChecklistItem[];
};

type FormMeta = { channels: ChannelOpt[]; category_choices: CatOpt[] };

const EMPTY_FORM = {
  name: "",
  channel_id: "",
  title_template: "{program} | {channel}",
  description_template: "",
  category_id: "",
  tags: "",
  privacy: "public",
  made_for_kids: false,
  default_language: "ja",
  default_audio_language: "ja",
  latency: "low",
  enable_dvr: true,
  enable_embed: true,
  enable_auto_start: false,
  enable_auto_stop: false,
  record_from_start: true,
  license: "youtube",
  public_stats_viewable: true,
  thumbnail_id: "",
  playlist_id: "",
  manual_checklist: [] as ChecklistItem[],
};

function detailToForm(d: PresetDetail): typeof EMPTY_FORM {
  return {
    name: d.name,
    channel_id: d.channel_id != null ? String(d.channel_id) : "",
    title_template: d.title_template,
    description_template: d.description_template,
    category_id: d.category_id != null ? String(d.category_id) : "",
    tags: d.tags.join(", "),
    privacy: d.privacy,
    made_for_kids: d.made_for_kids,
    default_language: d.default_language,
    default_audio_language: d.default_audio_language,
    latency: d.latency,
    enable_dvr: d.enable_dvr,
    enable_embed: d.enable_embed,
    enable_auto_start: d.enable_auto_start,
    enable_auto_stop: d.enable_auto_stop,
    record_from_start: d.record_from_start,
    license: d.license,
    public_stats_viewable: d.public_stats_viewable,
    thumbnail_id: d.thumbnail_id != null ? String(d.thumbnail_id) : "",
    playlist_id: d.playlist_id,
    manual_checklist: d.manual_checklist,
  };
}

function formToPayload(f: typeof EMPTY_FORM) {
  return {
    name: f.name.trim(),
    channel_id: f.channel_id ? Number(f.channel_id) : null,
    title_template: f.title_template,
    description_template: f.description_template,
    category_id: f.category_id ? Number(f.category_id) : null,
    tags: f.tags.split(",").map((t) => t.trim()).filter(Boolean),
    privacy: f.privacy,
    made_for_kids: f.made_for_kids,
    default_language: f.default_language,
    default_audio_language: f.default_audio_language,
    latency: f.latency,
    enable_dvr: f.enable_dvr,
    enable_embed: f.enable_embed,
    enable_auto_start: f.enable_auto_start,
    enable_auto_stop: f.enable_auto_stop,
    record_from_start: f.record_from_start,
    license: f.license,
    public_stats_viewable: f.public_stats_viewable,
    thumbnail_id: f.thumbnail_id ? Number(f.thumbnail_id) : null,
    playlist_id: f.playlist_id,
    manual_checklist: f.manual_checklist,
  };
}

function BoolField({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: ".3rem", fontSize: ".82rem", marginRight: ".8rem", cursor: "pointer" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

function PresetForm({
  init,
  meta,
  onSave,
  onCancel,
  busy,
}: {
  init: typeof EMPTY_FORM;
  meta: FormMeta;
  onSave: (payload: ReturnType<typeof formToPayload>) => Promise<void>;
  onCancel: () => void;
  busy: boolean;
}) {
  const [f, setF] = useState(init);
  const set = <K extends keyof typeof EMPTY_FORM>(k: K, v: (typeof EMPTY_FORM)[K]) =>
    setF((p) => ({ ...p, [k]: v }));

  return (
    <div className="card" style={{ marginBottom: ".8rem" }}
      onKeyDown={(e) => { if (e.key === "Enter" && (e.target as HTMLElement).tagName !== "TEXTAREA") e.preventDefault(); }}>

      {/* 基本 */}
      <p style={{ margin: "0 0 .5rem", fontSize: ".82rem", fontWeight: 600, color: "var(--muted)" }}>基本</p>
      <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap", marginBottom: ".5rem" }}>
        <label className="field" style={{ flex: "1 1 200px" }}>
          <span>プリセット名 *</span>
          <input value={f.name} onChange={(e) => set("name", e.target.value)} required placeholder="レギュラー番組A" />
        </label>
        <label className="field" style={{ flex: "1 1 160px" }}>
          <span>チャンネル</span>
          <select value={f.channel_id} onChange={(e) => set("channel_id", e.target.value)}>
            <option value="">（全チャンネル共通）</option>
            {meta.channels.map((c) => <option key={c.id} value={String(c.id)}>{c.name}</option>)}
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 120px" }}>
          <span>公開設定</span>
          <select value={f.privacy} onChange={(e) => set("privacy", e.target.value)}>
            <option value="public">公開</option>
            <option value="unlisted">限定公開</option>
            <option value="private">非公開</option>
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 120px" }}>
          <span>レイテンシ</span>
          <select value={f.latency} onChange={(e) => set("latency", e.target.value)}>
            <option value="normal">通常</option>
            <option value="low">低レイテンシ</option>
            <option value="ultraLow">超低遅延</option>
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 160px" }}>
          <span>カテゴリ</span>
          <select value={f.category_id} onChange={(e) => set("category_id", e.target.value)}>
            {meta.category_choices.map((c) => (
              <option key={c.value ?? ""} value={c.value != null ? String(c.value) : ""}>{c.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* タイトル・説明 */}
      <p style={{ margin: ".6rem 0 .4rem", fontSize: ".82rem", fontWeight: 600, color: "var(--muted)" }}>タイトル・説明テンプレート</p>
      <p style={{ margin: "0 0 .4rem", fontSize: ".75rem", color: "var(--muted)" }}>
        プレースホルダ: <code>{"{program}"}</code> 番組名&nbsp;/&nbsp;
        <code>{"{channel}"}</code> チャンネル名&nbsp;/&nbsp;
        <code>{"{date}"}</code> 放送日&nbsp;/&nbsp;
        <code>{"{start}"}</code> 開始時刻&nbsp;/&nbsp;
        <code>{"{end}"}</code> 終了時刻
      </p>
      <label className="field" style={{ display: "block", marginBottom: ".4rem" }}>
        <span>タイトルテンプレート</span>
        <input value={f.title_template} onChange={(e) => set("title_template", e.target.value)}
          placeholder="{program} | {channel}" style={{ width: "100%", maxWidth: "40rem" }} />
      </label>
      <label className="field" style={{ display: "block", marginBottom: ".5rem" }}>
        <span>説明テンプレート</span>
        <textarea value={f.description_template} onChange={(e) => set("description_template", e.target.value)}
          rows={5} style={{ width: "100%", maxWidth: "40rem", fontFamily: "monospace", fontSize: ".83rem" }} />
      </label>

      {/* YouTube API 設定 */}
      <p style={{ margin: ".6rem 0 .4rem", fontSize: ".82rem", fontWeight: 600, color: "var(--muted)" }}>YouTube API 設定</p>
      <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap", marginBottom: ".5rem" }}>
        <label className="field" style={{ flex: "1 1 200px" }}>
          <span>タグ (カンマ区切り)</span>
          <input value={f.tags} onChange={(e) => set("tags", e.target.value)} placeholder="ゲーム, VRChat" />
        </label>
        <label className="field" style={{ flex: "1 1 100px" }}>
          <span>言語</span>
          <input value={f.default_language} onChange={(e) => set("default_language", e.target.value)} placeholder="ja" />
        </label>
        <label className="field" style={{ flex: "1 1 100px" }}>
          <span>音声言語</span>
          <input value={f.default_audio_language} onChange={(e) => set("default_audio_language", e.target.value)} placeholder="ja" />
        </label>
      </div>
      <div style={{ marginBottom: ".5rem" }}>
        <BoolField label="DVR 有効" checked={f.enable_dvr} onChange={(v) => set("enable_dvr", v)} />
        <BoolField label="埋め込み許可" checked={f.enable_embed} onChange={(v) => set("enable_embed", v)} />
        <BoolField label="自動開始" checked={f.enable_auto_start} onChange={(v) => set("enable_auto_start", v)} />
        <BoolField label="自動終了" checked={f.enable_auto_stop} onChange={(v) => set("enable_auto_stop", v)} />
        <BoolField label="開始から録画" checked={f.record_from_start} onChange={(v) => set("record_from_start", v)} />
        <BoolField label="子供向け" checked={f.made_for_kids} onChange={(v) => set("made_for_kids", v)} />
        <BoolField label="統計を公開" checked={f.public_stats_viewable} onChange={(v) => set("public_stats_viewable", v)} />
      </div>
      <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap", marginBottom: ".5rem" }}>
        <label className="field" style={{ flex: "1 1 160px" }}>
          <span>ライセンス</span>
          <select value={f.license} onChange={(e) => set("license", e.target.value)}>
            <option value="youtube">YouTube 標準</option>
            <option value="creativeCommon">クリエイティブ・コモンズ</option>
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 160px" }}>
          <span>再生リスト ID</span>
          <input value={f.playlist_id} onChange={(e) => set("playlist_id", e.target.value)} placeholder="PLxxxxxx" />
        </label>
        <label className="field" style={{ flex: "1 1 120px" }}>
          <span>サムネイル Asset ID</span>
          <input type="number" min={1} value={f.thumbnail_id} onChange={(e) => set("thumbnail_id", e.target.value)}
            placeholder="（未設定）" style={{ width: "8rem" }} />
        </label>
      </div>

      {/* 手動チェックリスト定義 */}
      {f.manual_checklist.length > 0 && (
        <>
          <p style={{ margin: ".6rem 0 .4rem", fontSize: ".82rem", fontWeight: 600, color: "var(--muted)" }}>
            手動チェックリスト既定値 <span style={{ fontWeight: 400, fontSize: ".75rem" }}>(配信ごとの初期チェック状態)</span>
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: ".2rem .8rem" }}>
            {f.manual_checklist.map((item, idx) => (
              <label key={item.key} style={{ display: "flex", alignItems: "center", gap: ".3rem", fontSize: ".81rem", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={item.default}
                  onChange={(e) => {
                    const next = [...f.manual_checklist];
                    next[idx] = { ...item, default: e.target.checked };
                    set("manual_checklist", next);
                  }}
                />
                {item.label}
              </label>
            ))}
          </div>
        </>
      )}

      <div style={{ display: "flex", gap: ".5rem", marginTop: ".8rem" }}>
        <button className="btn" type="button" disabled={busy || !f.name.trim()}
          onClick={() => void onSave(formToPayload(f))}>保存</button>
        <button className="btn secondary" type="button" onClick={onCancel}>キャンセル</button>
      </div>
    </div>
  );
}

export function YouTubePresetsPage() {
  const [items, setItems] = useState<PresetListItem[]>([]);
  const [meta, setMeta] = useState<FormMeta | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<typeof EMPTY_FORM | null>(null);

  async function loadList() {
    setErr("");
    const res = await fetch(BASE, { credentials: "same-origin" });
    if (!res.ok) { setErr("読み込み失敗 (staff 権限が必要)"); return; }
    setItems(await res.json());
  }

  async function loadMeta() {
    if (meta) return;
    const res = await fetch(`${BASE}/form`, { credentials: "same-origin" });
    if (!res.ok) return;
    setMeta(await res.json());
  }

  useEffect(() => { void loadList(); }, []);

  async function startCreate() {
    await loadMeta();
    setCreating(true);
    setEditingId(null);
    setEditForm(null);
  }

  async function startEdit(id: number) {
    await loadMeta();
    const res = await fetch(`${BASE}/${id}`, { credentials: "same-origin" });
    if (!res.ok) { setErr("プリセット読み込み失敗"); return; }
    const detail: PresetDetail = await res.json();
    setEditForm(detailToForm(detail));
    setEditingId(id);
    setCreating(false);
  }

  async function save(payload: ReturnType<typeof formToPayload>, id?: number) {
    setBusy(true); setErr(""); setMsg("");
    const url = id ? `${BASE}/${id}` : BASE;
    const { status } = await postJson(url, payload);
    setBusy(false);
    if (status === 200) {
      setMsg(id ? "更新しました" : "作成しました");
      setCreating(false); setEditingId(null); setEditForm(null);
      void loadList();
    } else setErr("保存に失敗しました");
  }

  async function del(item: PresetListItem) {
    if (!window.confirm(`「${item.name}」を削除しますか？`)) return;
    setBusy(true); setErr(""); setMsg("");
    const res = await fetch(`${BASE}/${item.id}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRFToken": readCookie("csrftoken") },
    });
    setBusy(false);
    if (res.ok) { setMsg("削除しました"); setItems((prev) => prev.filter((x) => x.id !== item.id)); }
    else setErr("削除に失敗しました");
  }

  const cancelForm = () => { setCreating(false); setEditingId(null); setEditForm(null); };

  return (
    <StudioPage
      title="配信プリセット管理"
      actions={
        !creating && editingId == null
          ? <button className="btn" type="button" onClick={() => void startCreate()}>＋ プリセットを追加</button>
          : undefined
      }
    >
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}

      {creating && meta && (
        <PresetForm
          init={{ ...EMPTY_FORM }}
          meta={meta}
          onSave={(p) => save(p)}
          onCancel={cancelForm}
          busy={busy}
        />
      )}

      {items.length === 0 && !creating && (
        <p className="muted">プリセットがありません。「＋ プリセットを追加」で作成してください。</p>
      )}

      {items.map((item) => (
        <div key={item.id}>
          {editingId === item.id && editForm && meta ? (
            <PresetForm
              init={editForm}
              meta={meta}
              onSave={(p) => save(p, item.id)}
              onCancel={cancelForm}
              busy={busy}
            />
          ) : (
            <section className="card" style={{ marginBottom: ".5rem" }}>
              <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline", flexWrap: "wrap" }}>
                <strong style={{ fontSize: ".95rem" }}>{item.name}</strong>
                <span className="muted" style={{ fontSize: ".78rem" }}>{item.channel_name ?? "（全ch共通）"}</span>
                <span className="muted" style={{ fontSize: ".78rem" }}>
                  {item.privacy} · {item.latency}{item.category_id != null ? ` · cat:${item.category_id}` : ""}
                </span>
                <span style={{ marginLeft: "auto", display: "flex", gap: ".4rem" }}>
                  <button className="btn secondary" type="button" style={{ fontSize: ".78rem", padding: ".1rem .5rem" }}
                    disabled={busy} onClick={() => void startEdit(item.id)}>編集</button>
                  <button className="btn danger" type="button" style={{ fontSize: ".78rem", padding: ".1rem .5rem" }}
                    disabled={busy} onClick={() => void del(item)}>削除</button>
                </span>
              </div>
            </section>
          )}
        </div>
      ))}
    </StudioPage>
  );
}
