// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** シリーズ 作成/編集ハブ (旧 Django series_list/series_edit/slot_form の SPA 化)。
 *
 * URL: /series/:slug/new (作成) / /series/:slug/edit/:seriesId (編集・タブ)
 * 検証は admin_series が既存 SeriesForm/SeriesSlotForm をサーバ側で再利用するため、ここは入力 UI に専念。
 * タブ: 基本情報 / スロット (繰り返し+展開プレビュー) / 投書フォーム / 回(Episode)。作成時は基本情報のみ。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Notice, StudioPage } from "../atoms";

import type { EpisodeOut, SeriesFormOut, SeriesOut, SlotFormOut } from "../hooks";
import { postFile, postForm, postJson } from "../hooks";
import { ClockStyleOverride } from "../ClockStyleOverride";
import { RouterLink } from "../links";
import { SeriesFormsPanel } from "./SeriesFormsPage";
import { SeriesPostsPanel } from "./SeriesPostsPage";

type Tab = "basic" | "slots" | "forms" | "posts" | "episodes";

function errDetail(error: unknown, fallback: string): string {
  return (error as { detail?: string })?.detail || fallback;
}

// --------------------------------------------------------------------------- //
//  基本情報タブ                                                                //
// --------------------------------------------------------------------------- //

function BasicTab({
  slug,
  seriesId,
  onCreated,
  onDeleted,
}: {
  slug: string;
  seriesId: string;
  onCreated: (id: number) => void;
  onDeleted: () => void;
}) {
  const isNew = !seriesId;
  const [d, setD] = useState<SeriesFormOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // YouTube preset インライン作成
  const [showPresetCreate, setShowPresetCreate] = useState(false);
  const [presetF, setPresetF] = useState({
    name: "",
    title_template: "{program} | {channel}",
    description_template: "",
    privacy: "public",
  });
  // テンプレートから読み込む
  type YtTmpl = { id: number; name: string; title_template: string; description_template: string };
  const [ytTmpls, setYtTmpls] = useState<YtTmpl[]>([]);
  const [tmplsLoaded, setTmplsLoaded] = useState(false);
  const [f, setF] = useState({
    title: "",
    slug: "",
    genre: "",
    rating: "",
    exposure_policy_default: "public",
    description: "",
    cast: "",
    thumbnail_url: "",
    x_handle: "",
    x_hashtag: "",
    is_active: true,
    clock_hidden: false,
    lbar_hidden: false,
    clock_style_override: null as Record<string, unknown> | null,
    youtube_dedicated: false,
    youtube_preset_id: "" as string,
  });

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/scheduling/{slug}/series-form", {
        params: { path: { slug }, query: seriesId ? { series_id: Number(seriesId) } : {} },
      })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗 (staff 権限が必要)");
        setD(data);
        const i = data.initial;
        setF({
          title: i.title ?? "",
          slug: i.slug ?? "",
          genre: i.genre ?? "",
          rating: i.rating ?? "",
          exposure_policy_default: i.exposure_policy_default ?? "public",
          description: i.description ?? "",
          cast: i.cast ?? "",
          thumbnail_url: i.thumbnail_url ?? "",
          x_handle: i.x_handle ?? "",
          x_hashtag: i.x_hashtag ?? "",
          is_active: i.is_active ?? true,
          clock_hidden: i.clock_hidden ?? false,
          lbar_hidden: i.lbar_hidden ?? false,
          clock_style_override: (i.clock_style_override as Record<string, unknown> | null | undefined) ?? null,
          youtube_dedicated: i.youtube_dedicated ?? false,
          youtube_preset_id: i.youtube_preset_id ? String(i.youtube_preset_id) : "",
        });
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug, seriesId]);
  useEffect(load, [load]);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) {
    setF((prev) => ({ ...prev, [k]: v }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const body = {
      title: f.title,
      slug: f.slug,
      genre: f.genre,
      rating: f.rating,
      exposure_policy_default: f.exposure_policy_default,
      description: f.description,
      cast: f.cast,
      thumbnail_url: f.thumbnail_url,
      x_handle: f.x_handle,
      x_hashtag: f.x_hashtag,
      is_active: f.is_active,
      clock_hidden: f.clock_hidden,
      lbar_hidden: f.lbar_hidden,
      clock_style_override: f.clock_style_override,
      youtube_dedicated: f.youtube_dedicated,
      youtube_preset_id: f.youtube_preset_id ? Number(f.youtube_preset_id) : null,
    };
    const req = isNew
      ? api.POST("/api/v1/admin/scheduling/{slug}/series-full", { params: { path: { slug } }, body })
      : api.POST("/api/v1/admin/scheduling/{slug}/series/{series_id}", {
          params: { path: { slug, series_id: Number(seriesId) } },
          body,
        });
    const { data, error } = await req;
    if (error) { setBusy(false); return setErr(errDetail(error, "保存に失敗しました")); }
    if (isNew && data?.id) {
      if (pendingFile) {
        const uploadUrl = `/api/v1/admin/scheduling/${slug}/series/${data.id}/thumbnail`;
        await postFile(uploadUrl, { file: pendingFile });
      }
      setBusy(false);
      return onCreated(data.id);
    }
    setBusy(false);
    setMsg("保存しました");
  }

  async function uploadThumb(file: File) {
    if (!d?.thumbnail_post_url) return;
    setBusy(true);
    setErr("");
    const { status, data } = await postFile(d.thumbnail_post_url, { file });
    setBusy(false);
    if (status === 200) {
      try {
        set("thumbnail_url", JSON.parse(data).thumbnail_url);
        setMsg("サムネ画像を更新しました");
        if (fileInputRef.current) fileInputRef.current.value = "";
      } catch {
        setErr("アップロードに失敗しました (レスポンス解析エラー)");
      }
    } else {
      let msg = "アップロードに失敗しました";
      try { msg = JSON.parse(data).detail || msg; } catch { /* keep default */ }
      setErr(msg);
    }
  }

  async function openPresetCreate() {
    setShowPresetCreate(true);
    if (!tmplsLoaded) {
      const res = await fetch("/api/v1/admin/youtube/description-templates", { credentials: "same-origin" });
      if (res.ok) setYtTmpls(await res.json());
      setTmplsLoaded(true);
    }
  }

  function applyTemplate(id: string) {
    const t = ytTmpls.find((x) => String(x.id) === id);
    if (!t) return;
    setPresetF((p) => ({ ...p, title_template: t.title_template, description_template: t.description_template }));
  }

  async function createPreset() {
    if (busy || !presetF.name.trim()) return;
    setBusy(true);
    setErr("");
    const { status, data } = await postJson(`/api/v1/admin/scheduling/${slug}/youtube-preset`, presetF);
    setBusy(false);
    if (status === 200) {
      const p = data as { id: number; name: string };
      setD((prev) => prev ? { ...prev, youtube_presets: [...prev.youtube_presets, p] } : prev);
      set("youtube_preset_id", String(p.id));
      setShowPresetCreate(false);
      setPresetF({ name: "", title_template: "{channel} {date} {start}-{end}", description_template: "", privacy: "public" });
    } else setErr("プリセットの作成に失敗しました");
  }

  async function del() {
    if (!d?.delete_url || busy) return;
    if (!window.confirm("このシリーズを削除しますか？ (展開済みの Program は残ります)")) return;
    setBusy(true);
    const { status } = await postForm(d.delete_url);
    setBusy(false);
    if (status === 200 || status === 204 || status === 302) onDeleted();
    else setErr("削除に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <div style={{ maxWidth: "44rem" }}>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}
      <form onSubmit={submit} className="card">
        <label className="field" style={{ display: "block" }}><span>タイトル</span>
          <input value={f.title} onChange={(e) => set("title", e.target.value)} required style={{ width: "100%" }} />
        </label>
        <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
          <label className="field"><span>スラッグ (公開 URL・空=自動)</span>
            <input value={f.slug} onChange={(e) => set("slug", e.target.value)} pattern="[-a-z0-9_]*" placeholder="morning-show" />
          </label>
          <label className="field"><span>ジャンル</span>
            <select value={f.genre} onChange={(e) => set("genre", e.target.value)}>
              <option value="">—</option>
              {d.genre_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>
          <label className="field"><span>視聴年齢制限</span>
            <select value={f.rating} onChange={(e) => set("rating", e.target.value)}>
              <option value="">全年齢</option>
              {d.rating_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>
          <label className="field"><span>配信ポリシー既定 (exposure_policy_default)</span>
            <select value={f.exposure_policy_default} onChange={(e) => set("exposure_policy_default", e.target.value)}>
              {d.exposure_policy_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>
        </div>
        {f.exposure_policy_default === "members_yt_site" && (
          <p style={{ border: "1px solid var(--warn)", borderRadius: ".3rem", padding: ".5rem .7rem", fontSize: ".8rem" }}>
            ⚠ 会員限定(YT+サイト)を既定にすると、この番組枠から展開される全回が対象になります。
            YouTube メンバー限定ミラー専用の CasparCG チャンネル/エンコードが送出ノードに前提です
            (GPU 容量に専用 GPU が実質必須、members-only 配信の永続 1 本を YouTube Studio で手動作成済みであること)。
          </p>
        )}
        <label className="field" style={{ display: "block" }}><span>あらすじ</span>
          <textarea value={f.description} onChange={(e) => set("description", e.target.value)} rows={2} style={{ width: "100%" }} />
        </label>
        <label className="field" style={{ display: "block" }}><span>出演者 (カンマ/改行区切り)</span>
          <textarea value={f.cast} onChange={(e) => set("cast", e.target.value)} rows={2} style={{ width: "100%" }} />
        </label>
        <label className="field" style={{ display: "block" }}><span>サムネ画像 URL (推奨 PC 1920×720 / SP 1280×720・横中央)</span>
          <input value={f.thumbnail_url} onChange={(e) => set("thumbnail_url", e.target.value)} placeholder="https:// または下のアップロード" style={{ width: "100%" }} />
        </label>
        <div style={{ display: "flex", gap: ".6rem", alignItems: "center", flexWrap: "wrap" }}>
          {!isNew && f.thumbnail_url && <img src={f.thumbnail_url} alt="" style={{ height: "3rem", borderRadius: ".3rem" }} />}
          <label className="field">
            <span>{isNew ? "画像をアップロード（作成後に自動適用）" : "画像をアップロード"}</span>
            <input ref={fileInputRef} type="file" accept="image/jpeg,image/png,image/webp,image/gif"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (isNew) setPendingFile(file);
                else void uploadThumb(file);
              }} />
          </label>
          {isNew && pendingFile && (
            <span className="muted" style={{ fontSize: ".75rem" }}>
              {pendingFile.name}（作成時に自動アップロード）
              <button type="button" style={{ marginLeft: ".4rem", background: "none", border: "none", color: "var(--warn)", cursor: "pointer" }} onClick={() => { setPendingFile(null); if (fileInputRef.current) fileInputRef.current.value = ""; }}>×</button>
            </span>
          )}
        </div>
        <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
          <label className="field"><span>X アカウント</span>
            <input value={f.x_handle} onChange={(e) => set("x_handle", e.target.value)} placeholder="@なし可" />
          </label>
          <label className="field"><span>X ハッシュタグ</span>
            <input value={f.x_hashtag} onChange={(e) => set("x_hashtag", e.target.value)} placeholder="#なし可" />
          </label>
          <div className="field">
            <span>YouTube 専用枠 preset</span>
            <div style={{ display: "flex", gap: ".4rem", alignItems: "center" }}>
              <select value={f.youtube_preset_id} onChange={(e) => set("youtube_preset_id", e.target.value)}>
                <option value="">—</option>
                {d.youtube_presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <button type="button" className={`btn${showPresetCreate ? " secondary" : ""}`} style={{ fontSize: ".76rem", padding: ".1rem .5rem", whiteSpace: "nowrap" }}
                onClick={() => showPresetCreate ? setShowPresetCreate(false) : void openPresetCreate()}>
                {showPresetCreate ? "キャンセル" : "＋新規"}
              </button>
            </div>
          </div>
        </div>
        {showPresetCreate && (
          <div className="card" style={{ background: "#1e2228", margin: ".6rem 0" }}
            onKeyDown={(e) => { if (e.key === "Enter") e.stopPropagation(); }}>
            <p style={{ margin: "0 0 .5rem", fontSize: ".8rem", fontWeight: 600 }}>YouTube preset を新規作成</p>
            {ytTmpls.length > 0 && (
              <label className="field" style={{ marginBottom: ".6rem" }}>
                <span style={{ fontSize: ".76rem" }}>テンプレートから読み込む</span>
                <select defaultValue="" onChange={(e) => applyTemplate(e.target.value)} style={{ maxWidth: "22rem" }}>
                  <option value="">— 選択して流し込む —</option>
                  {ytTmpls.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
            )}
            <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap" }}>
              <label className="field"><span>プリセット名 *</span>
                <input value={presetF.name} onChange={(e) => setPresetF((p) => ({ ...p, name: e.target.value }))} placeholder="レギュラー枠A" style={{ width: "14rem" }} />
              </label>
              <label className="field"><span>公開設定</span>
                <select value={presetF.privacy} onChange={(e) => setPresetF((p) => ({ ...p, privacy: e.target.value }))}>
                  <option value="public">公開</option>
                  <option value="unlisted">限定公開</option>
                  <option value="private">非公開</option>
                </select>
              </label>
            </div>
            <label className="field" style={{ display: "block" }}><span>タイトルテンプレート</span>
              <input value={presetF.title_template} onChange={(e) => setPresetF((p) => ({ ...p, title_template: e.target.value }))} style={{ width: "100%" }} placeholder="{program} | {channel}" />
            </label>
            <label className="field" style={{ display: "block" }}><span>説明テンプレート</span>
              <textarea value={presetF.description_template} onChange={(e) => setPresetF((p) => ({ ...p, description_template: e.target.value }))} rows={5} style={{ width: "100%", fontFamily: "monospace", fontSize: ".83rem" }} />
            </label>
            <p className="muted" style={{ fontSize: ".72rem", margin: ".2rem 0 .4rem" }}>プレースホルダ: {"{program}"} {"{channel}"} {"{date}"} {"{start}"} {"{end}"}</p>
            <button className="btn" type="button" disabled={busy || !presetF.name.trim()} onClick={() => void createPreset()} style={{ marginTop: ".4rem", minWidth: "8rem" }}>作成して選択</button>
          </div>
        )}
        <div style={{ display: "flex", gap: "1rem", alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}><input type="checkbox" checked={f.is_active} onChange={(e) => set("is_active", e.target.checked)} /> 有効</label>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}><input type="checkbox" checked={f.clock_hidden} onChange={(e) => set("clock_hidden", e.target.checked)} /> 朝・夕の時計を出さない</label>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}><input type="checkbox" checked={f.lbar_hidden} onChange={(e) => set("lbar_hidden", e.target.checked)} /> L字を出さない</label>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}><input type="checkbox" checked={f.youtube_dedicated} onChange={(e) => set("youtube_dedicated", e.target.checked)} /> YouTube 専用枠を立てる</label>
        </div>
        <ClockStyleOverride
          value={f.clock_style_override}
          onChange={(v) => set("clock_style_override", v)}
        />
        <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem" }}>
          <button className="btn" type="submit" disabled={busy}>{isNew ? "作成" : "保存"}</button>
          {!isNew && d.delete_url && (
            <button className="btn" type="button" onClick={() => void del()} disabled={busy} style={{ background: "var(--warn)" }}>削除</button>
          )}
        </div>
      </form>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  スロットタブ                                                                //
// --------------------------------------------------------------------------- //

const RECUR_PARAM_FIELD: Record<string, "weeks_csv" | "days_csv" | "ending_csv" | null> = {
  weekly: null,
  daily: null,
  monthly_nth_dow: "weeks_csv",
  days_of_month: "days_csv",
  days_ending: "ending_csv",
};

function SlotForm({
  slug,
  seriesId,
  slotId,
  onDone,
  onCancel,
}: {
  slug: string;
  seriesId: string;
  slotId: number | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [d, setD] = useState<SlotFormOut | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<string[] | null>(null);
  const [f, setF] = useState({
    dow: "0",
    start_time: "20:00",
    duration_min: "60",
    program_type: "recorded",
    default_asset_id: "",
    live_source_id: "",
    effective_from: "",
    effective_to: "",
    recurrence_kind: "weekly",
    weeks_csv: "",
    days_csv: "",
    ending_csv: "",
  });

  useEffect(() => {
    api
      .GET("/api/v1/admin/scheduling/{slug}/series/{series_id}/slot-form", {
        params: { path: { slug, series_id: Number(seriesId) }, query: slotId ? { slot_id: slotId } : {} },
      })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗");
        setD(data);
        const i = data.initial;
        setF({
          dow: String(i.dow ?? 0),
          start_time: i.start_time || "20:00",
          duration_min: String(i.duration_min ?? 60),
          program_type: i.program_type || "recorded",
          default_asset_id: i.default_asset_id ? String(i.default_asset_id) : "",
          live_source_id: i.live_source_id ? String(i.live_source_id) : "",
          effective_from: i.effective_from || "",
          effective_to: i.effective_to || "",
          recurrence_kind: i.recurrence_kind || "weekly",
          weeks_csv: i.weeks_csv || "",
          days_csv: i.days_csv || "",
          ending_csv: i.ending_csv || "",
        });
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug, seriesId, slotId]);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) {
    setF((prev) => ({ ...prev, [k]: v }));
    setPreview(null);
  }

  function body() {
    const isLive = f.program_type === "live";
    return {
      dow: Number(f.dow),
      start_time: f.start_time,
      duration_min: Number(f.duration_min),
      program_type: f.program_type,
      default_asset_id: !isLive && f.default_asset_id ? Number(f.default_asset_id) : null,
      live_source_id: isLive && f.live_source_id ? Number(f.live_source_id) : null,
      effective_from: f.effective_from,
      effective_to: f.effective_to,
      recurrence_kind: f.recurrence_kind,
      weeks_csv: f.weeks_csv,
      days_csv: f.days_csv,
      ending_csv: f.ending_csv,
    };
  }

  async function runPreview() {
    setErr("");
    const { data, error } = await api.POST(
      "/api/v1/admin/scheduling/{slug}/series/{series_id}/slots/preview",
      { params: { path: { slug, series_id: Number(seriesId) } }, body: body() }
    );
    if (error) return setErr(errDetail(error, "プレビューに失敗しました"));
    setPreview((data as string[]) ?? []);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    const req = slotId
      ? api.POST("/api/v1/admin/scheduling/{slug}/slots/{slot_id}", { params: { path: { slug, slot_id: slotId } }, body: body() })
      : api.POST("/api/v1/admin/scheduling/{slug}/series/{series_id}/slots", { params: { path: { slug, series_id: Number(seriesId) } }, body: body() });
    const { error } = await req;
    setBusy(false);
    if (error) return setErr(errDetail(error, "保存に失敗しました"));
    onDone();
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;
  const isLive = f.program_type === "live";
  const paramField = RECUR_PARAM_FIELD[f.recurrence_kind] ?? null;

  return (
    <form onSubmit={submit} className="card" style={{ marginBottom: ".8rem" }}>
      {err && <Notice variant="error">{err}</Notice>}
      <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
        <label className="field"><span>曜日</span>
          <select value={f.dow} onChange={(e) => set("dow", e.target.value)}>
            {d.dow_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </label>
        <label className="field"><span>開始時刻</span>
          <input type="time" value={f.start_time} onChange={(e) => set("start_time", e.target.value)} required />
        </label>
        <label className="field"><span>尺(分)</span>
          <input type="number" min={1} value={f.duration_min} onChange={(e) => set("duration_min", e.target.value)} required style={{ width: "5rem" }} />
        </label>
        <label className="field"><span>種別</span>
          <select value={f.program_type} onChange={(e) => set("program_type", e.target.value)}>
            <option value="recorded">◇ 録画</option><option value="live">◆ 生</option>
          </select>
        </label>
      </div>
      {!isLive ? (
        <label className="field" style={{ display: "block" }}><span>既定素材 (録画)</span>
          <select value={f.default_asset_id} onChange={(e) => set("default_asset_id", e.target.value)} style={{ width: "100%" }}>
            <option value="">— 素材を選択 —</option>
            {d.assets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </label>
      ) : (
        <label className="field" style={{ display: "block" }}><span>ライブソース (生)</span>
          <select value={f.live_source_id} onChange={(e) => set("live_source_id", e.target.value)} style={{ width: "100%" }}>
            <option value="">— ソースを選択 —</option>
            {d.live_sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
      )}
      <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
        <label className="field"><span>繰り返し</span>
          <select value={f.recurrence_kind} onChange={(e) => set("recurrence_kind", e.target.value)}>
            {d.recurrence_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </label>
        {paramField === "weeks_csv" && (
          <label className="field"><span>第N (例 2,4)</span><input value={f.weeks_csv} onChange={(e) => set("weeks_csv", e.target.value)} placeholder="2,4" /></label>
        )}
        {paramField === "days_csv" && (
          <label className="field"><span>日 (例 1,15)</span><input value={f.days_csv} onChange={(e) => set("days_csv", e.target.value)} placeholder="1,15" /></label>
        )}
        {paramField === "ending_csv" && (
          <label className="field"><span>末尾 (例 5)</span><input value={f.ending_csv} onChange={(e) => set("ending_csv", e.target.value)} placeholder="5" /></label>
        )}
      </div>
      <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
        <label className="field"><span>有効開始</span><input type="date" value={f.effective_from} onChange={(e) => set("effective_from", e.target.value)} required /></label>
        <label className="field"><span>有効終了 (任意)</span><input type="date" value={f.effective_to} onChange={(e) => set("effective_to", e.target.value)} /></label>
      </div>
      {preview && (
        <div className="muted" style={{ fontSize: ".78rem", marginTop: ".4rem" }}>
          {preview.length ? <>次回予定: {preview.join(" / ")}</> : <>該当日なし (有効期間/パターンを確認)</>}
        </div>
      )}
      <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
        <button className="btn" type="submit" disabled={busy}>{slotId ? "保存" : "追加"}</button>
        <button className="btn" type="button" onClick={() => void runPreview()}>次回予定をプレビュー</button>
        <button className="btn secondary" type="button" onClick={onCancel}>キャンセル</button>
      </div>
    </form>
  );
}

function SlotsTab({ slug, seriesId }: { slug: string; seriesId: string }) {
  const [slots, setSlots] = useState<SeriesOut["series"][number]["slots"]>([]);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/scheduling/{slug}/series", { params: { path: { slug } } })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗");
        const row = data.series.find((s) => s.id === Number(seriesId));
        setSlots(row?.slots ?? []);
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug, seriesId]);
  useEffect(load, [load]);

  async function del(slotId: number) {
    if (busy || !window.confirm("このスロットを削除しますか？")) return;
    setBusy(true);
    const { error } = await api.DELETE("/api/v1/admin/scheduling/{slug}/slots/{slot_id}", {
      params: { path: { slug, slot_id: slotId } },
    });
    setBusy(false);
    if (error) setErr(errDetail(error, "削除に失敗しました"));
    else load();
  }

  return (
    <div>
      {err && <Notice variant="error">{err}</Notice>}
      {editing !== null ? (
        <SlotForm
          slug={slug}
          seriesId={seriesId}
          slotId={editing === "new" ? null : editing}
          onDone={() => { setEditing(null); load(); }}
          onCancel={() => setEditing(null)}
        />
      ) : (
        <button className="btn" type="button" style={{ marginBottom: ".8rem" }} onClick={() => setEditing("new")}>＋ スロットを追加</button>
      )}
      {slots.length === 0 && !editing && <p className="muted">スロットがありません。「＋ スロットを追加」で繰り返し編成を作成します。</p>}
      {slots.length > 0 && (
        <table>
          <thead><tr><th>繰り返し</th><th>時刻</th><th>尺</th><th>種別</th><th>ソース</th><th></th></tr></thead>
          <tbody>
            {slots.map((sl) => (
              <tr key={sl.id}>
                <td>{sl.recurrence}</td>
                <td style={{ whiteSpace: "nowrap" }}>{sl.start_time}</td>
                <td style={{ whiteSpace: "nowrap" }}>{sl.duration}</td>
                <td>{sl.program_type === "live" ? "◆生" : "◇録画"}</td>
                <td>{sl.source}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn" type="button" onClick={() => setEditing(sl.id)} style={{ fontSize: ".72rem", padding: "0 .4rem" }}>編集</button>
                  <button className="btn" type="button" disabled={busy} onClick={() => void del(sl.id)} style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".3rem", background: "var(--warn)" }}>×</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  回 (Episode) タブ                                                           //
// --------------------------------------------------------------------------- //

const EP_STATUS = [
  { value: "planned", label: "予定" },
  { value: "confirmed", label: "確定" },
  { value: "aired", label: "放送済" },
];

function EpisodesTab({ slug, seriesId }: { slug: string; seriesId: string }) {
  const [eps, setEps] = useState<EpisodeOut[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<EpisodeOut | "new" | null>(null);
  const [f, setF] = useState({ episode_no: "", air_date: "", title: "", status: "planned" });

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/scheduling/{slug}/series/{series_id}/episodes", {
        params: { path: { slug, series_id: Number(seriesId) } },
      })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗");
        setEps(data as EpisodeOut[]);
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug, seriesId]);
  useEffect(load, [load]);

  function startEdit(ep: EpisodeOut | "new") {
    setEditing(ep);
    setErr("");
    if (ep === "new") setF({ episode_no: "", air_date: "", title: "", status: "planned" });
    else setF({
      episode_no: ep.episode_no != null ? String(ep.episode_no) : "",
      air_date: ep.air_date ?? "",
      title: ep.title ?? "",
      status: ep.status ?? "planned",
    });
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    const reqBody = {
      episode_no: f.episode_no ? Number(f.episode_no) : null,
      air_date: f.air_date,
      title: f.title,
      status: f.status,
    };
    const req = editing === "new"
      ? api.POST("/api/v1/admin/scheduling/{slug}/series/{series_id}/episodes", { params: { path: { slug, series_id: Number(seriesId) } }, body: reqBody })
      : api.POST("/api/v1/admin/scheduling/{slug}/episodes/{episode_id}", { params: { path: { slug, episode_id: (editing as EpisodeOut).id } }, body: reqBody });
    const { error } = await req;
    setBusy(false);
    if (error) return setErr(errDetail(error, "保存に失敗しました"));
    setEditing(null);
    load();
  }

  async function del(ep: EpisodeOut) {
    if (busy || !window.confirm(`第${ep.episode_no ?? "?"}回を削除しますか？`)) return;
    setBusy(true);
    const { error } = await api.DELETE("/api/v1/admin/scheduling/{slug}/episodes/{episode_id}", {
      params: { path: { slug, episode_id: ep.id } },
    });
    setBusy(false);
    if (error) setErr(errDetail(error, "削除できません (素材確定済みの回は不可)"));
    else load();
  }

  return (
    <div>
      {err && <Notice variant="error">{err}</Notice>}
      <p className="muted" style={{ fontSize: ".75rem" }}>本編素材は納品 (delivery) で確定するため、ここでは予定回の確保とメタ編集のみ行います。</p>
      {editing !== null ? (
        <form onSubmit={save} className="card" style={{ marginBottom: ".8rem" }}>
          <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
            <label className="field"><span>回数</span><input type="number" value={f.episode_no} onChange={(e) => setF((p) => ({ ...p, episode_no: e.target.value }))} style={{ width: "5rem" }} /></label>
            <label className="field"><span>放送日</span><input type="date" value={f.air_date} onChange={(e) => setF((p) => ({ ...p, air_date: e.target.value }))} /></label>
            <label className="field"><span>状態</span>
              <select value={f.status} onChange={(e) => setF((p) => ({ ...p, status: e.target.value }))}>
                {EP_STATUS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </label>
          </div>
          <label className="field" style={{ display: "block" }}><span>タイトル</span><input value={f.title} onChange={(e) => setF((p) => ({ ...p, title: e.target.value }))} style={{ width: "100%" }} /></label>
          <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem" }}>
            <button className="btn" type="submit" disabled={busy}>{editing === "new" ? "追加" : "保存"}</button>
            <button className="btn secondary" type="button" onClick={() => setEditing(null)}>キャンセル</button>
          </div>
        </form>
      ) : (
        <button className="btn" type="button" style={{ marginBottom: ".8rem" }} onClick={() => startEdit("new")}>＋ 回を追加</button>
      )}
      {eps.length === 0 && !editing && <p className="muted">回がありません。</p>}
      {eps.length > 0 && (
        <table>
          <thead><tr><th>回</th><th>放送日</th><th>タイトル</th><th>状態</th><th>確定素材</th><th></th></tr></thead>
          <tbody>
            {eps.map((ep) => (
              <tr key={ep.id}>
                <td style={{ whiteSpace: "nowrap" }}>{ep.episode_no != null ? `第${ep.episode_no}回` : "—"}</td>
                <td style={{ whiteSpace: "nowrap" }}>{ep.air_date || "—"}</td>
                <td>{ep.title || "—"}</td>
                <td><span className="chip" style={{ fontSize: ".72rem" }}>{ep.status_label}</span></td>
                <td className="muted" style={{ fontSize: ".78rem" }}>{ep.asset_title || "—"}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn" type="button" onClick={() => startEdit(ep)} style={{ fontSize: ".72rem", padding: "0 .4rem" }}>編集</button>
                  {!ep.asset_id && (
                    <button className="btn" type="button" disabled={busy} onClick={() => void del(ep)} style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".3rem", background: "var(--warn)" }}>×</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  ハブ                                                                        //
// --------------------------------------------------------------------------- //

const TABS: { key: Tab; label: string }[] = [
  { key: "basic", label: "基本情報" },
  { key: "slots", label: "スロット" },
  { key: "forms", label: "投書フォーム" },
  { key: "posts", label: "投稿" },
  { key: "episodes", label: "回" },
];

export function SeriesDetailPage() {
  const { slug = "", seriesId = "" } = useParams();
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const isNew = !seriesId;
  const initialTab = (sp.get("tab") as Tab) || "basic";
  const [tab, setTab] = useState<Tab>(isNew ? "basic" : initialTab);
  // #27: 投稿タブの公開範囲セレクトに使う creator 紐付け (未紐付けは null=完全公開のみ選択可)
  const [creatorId, setCreatorId] = useState<number | null>(null);
  useEffect(() => {
    if (isNew || !seriesId) return;
    api
      .GET("/api/v1/admin/fanclub/series/{series_id}/creator", { params: { path: { series_id: Number(seriesId) } } })
      .then(({ data }) => setCreatorId((data as { creator_id: number | null } | undefined)?.creator_id ?? null));
  }, [isNew, seriesId]);

  return (
    <StudioPage
      title={isNew ? "シリーズを作成" : "シリーズを編集"}
      breadcrumb={<Breadcrumb items={[{ label: "週間編成", href: `/series/${slug}` }]} linkComponent={RouterLink} />}
    >
      {!isNew && (
        <div style={{ display: "flex", gap: ".4rem", marginBottom: ".9rem", flexWrap: "wrap" }}>
          {TABS.map((t) => (
            <button key={t.key} type="button" className="btn"
              onClick={() => setTab(t.key)}
              style={{ background: tab === t.key ? "var(--accent, #38bdf8)" : "#444", color: tab === t.key ? "var(--accent-on, #04101a)" : undefined }}>
              {t.label}
            </button>
          ))}
        </div>
      )}
      {tab === "basic" && (
        <BasicTab
          slug={slug}
          seriesId={seriesId}
          onCreated={(id) => navigate(`/series/${slug}/edit/${id}`)}
          onDeleted={() => navigate(`/series/${slug}`)}
        />
      )}
      {!isNew && tab === "slots" && <SlotsTab slug={slug} seriesId={seriesId} />}
      {!isNew && tab === "forms" && <SeriesFormsPanel slug={slug} seriesId={seriesId} />}
      {!isNew && tab === "posts" && <SeriesPostsPanel slug={slug} seriesId={seriesId} creatorId={creatorId} />}
      {!isNew && tab === "episodes" && <EpisodesTab slug={slug} seriesId={seriesId} />}
    </StudioPage>
  );
}
