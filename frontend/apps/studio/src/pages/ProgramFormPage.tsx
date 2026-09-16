// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Notice, StudioPage } from "../atoms";

import { ClockStyleOverride } from "../ClockStyleOverride";
import type { ProgramFormOut } from "../hooks";
import { postForm } from "../hooks";
import { RouterLink } from "../links";

/** 番組 作成/編集フォーム (#Phase2e-2)。タイムラインの create-by-drag / 編集 の遷移先。
 * ソース制約 (録画=素材必須 / 生=live_source 必須) はサーバの ProgramForm 検証に委ねる。 */
export function ProgramFormPage() {
  const { slug = "" } = useParams();
  const [sp] = useSearchParams();
  const programId = sp.get("program_id") ?? "";
  const navigate = useNavigate();
  const [d, setD] = useState<ProgramFormOut | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // 編集中フォーム状態 (initial から)
  const [f, setF] = useState({
    title: "",
    type: "recorded",
    genre: "",
    exposure_policy: "",
    start_at: "",
    end_at: "",
    asset_id: "",
    live_source_id: "",
    cast: "",
    description: "",
    public_visible: true,
    vod_visibility: "off",
    clock_style_override: null as Record<string, unknown> | null,
    record_live: false,
  });

  const load = useCallback(() => {
    if (!slug) return;
    const query: Record<string, string | number> = {};
    if (programId) query.program_id = Number(programId);
    if (sp.get("start")) query.start = sp.get("start")!;
    if (sp.get("end")) query.end = sp.get("end")!;
    api
      .GET("/api/v1/admin/scheduling/{slug}/program-form", { params: { path: { slug }, query } })
      .then(({ data, error }) => {
        if (error) return setErr("読み込み失敗 (staff 権限が必要)");
        if (!data) return;
        setD(data);
        const i = data.initial;
        setF({
          title: i.title ?? "",
          type: i.type ?? "recorded",
          genre: i.genre ?? "",
          exposure_policy: i.exposure_policy ?? "",
          start_at: i.start_at ?? "",
          end_at: i.end_at ?? "",
          asset_id: i.asset_id ? String(i.asset_id) : "",
          live_source_id: i.live_source_id ? String(i.live_source_id) : "",
          cast: i.cast ?? "",
          description: i.description ?? "",
          public_visible: i.public_visible ?? true,
          vod_visibility: i.vod_visibility ?? "off",
          clock_style_override: (i.clock_style_override as Record<string, unknown> | null | undefined) ?? null,
          record_live: i.record_live ?? false,
        });
      })
      .catch(() => setErr("読み込み失敗"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, programId]);
  useEffect(load, [load]);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) {
    setF((prev) => ({ ...prev, [k]: v }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    const body = {
      title: f.title,
      type: f.type,
      genre: f.genre,
      exposure_policy: f.exposure_policy,
      start_at: f.start_at,
      end_at: f.end_at,
      asset_id: f.type === "recorded" && f.asset_id ? Number(f.asset_id) : null,
      live_source_id: f.type === "live" && f.live_source_id ? Number(f.live_source_id) : null,
      cast: f.cast,
      description: f.description,
      public_visible: f.public_visible,
      vod_visibility: f.vod_visibility,
      clock_style_override: f.clock_style_override,
      record_live: f.type === "live" && f.record_live,
    };
    const req = programId
      ? api.POST("/api/v1/admin/scheduling/{slug}/programs/{program_id}", { params: { path: { slug, program_id: Number(programId) } }, body })
      : api.POST("/api/v1/admin/scheduling/{slug}/programs", { params: { path: { slug } }, body });
    const { error } = await req;
    setBusy(false);
    if (error) {
      setErr((error as { detail?: string })?.detail || "保存に失敗しました");
      return;
    }
    navigate(`/scheduling/${slug}`);
  }

  async function del() {
    if (!d?.delete_url || busy) return;
    if (!window.confirm("この番組を編成から削除しますか？")) return;
    setBusy(true);
    const { status } = await postForm(d.delete_url, {});
    setBusy(false);
    if (status === 200 || status === 204 || status === 302) navigate(`/scheduling/${slug}`);
    else setErr("削除に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;
  const isLive = f.type === "live";

  return (
    <StudioPage
      title={programId ? "番組を編集" : "番組を追加"}
      breadcrumb={
        <Breadcrumb items={[{ label: "タイムライン", href: `/scheduling/${slug}` }]} linkComponent={RouterLink} />
      }
      actions={
        <>
          <span className="muted">{d.channel.name}</span>
          {programId && (
            <RouterLink href={`/graphic-cues/${slug}/program/${programId}`} className="muted">自動グラフィック(CG)</RouterLink>
          )}
          {programId && isLive && (
            <RouterLink href={`/live-rundown/${slug}/${programId}`} className="muted">生キューシート</RouterLink>
          )}
        </>
      }
    >
      <div style={{ maxWidth: "44rem" }}>
        {err && <Notice variant="error">{err}</Notice>}
        <form onSubmit={submit} className="card">
        <label className="field" style={{ display: "block" }}><span>タイトル</span><input value={f.title} onChange={(e) => set("title", e.target.value)} required style={{ width: "100%" }} /></label>
        <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
          <label className="field"><span>種別</span>
            <select value={f.type} onChange={(e) => set("type", e.target.value)}><option value="recorded">◇ 録画</option><option value="live">◆ 生</option></select>
          </label>
          <label className="field"><span>ジャンル</span><input value={f.genre} onChange={(e) => set("genre", e.target.value)} /></label>
        </div>
        {!isLive ? (
          <label className="field" style={{ display: "block" }}><span>素材 (録画)</span>
            <select value={f.asset_id} onChange={(e) => set("asset_id", e.target.value)} style={{ width: "100%" }}>
              <option value="">— 素材を選択 —</option>
              {d.assets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
        ) : (
          <>
            <label className="field" style={{ display: "block" }}><span>ライブソース (生)</span>
              <select value={f.live_source_id} onChange={(e) => set("live_source_id", e.target.value)} style={{ width: "100%" }}>
                <option value="">— ソースを選択 —</option>
                {d.live_sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
            <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}>
              <input type="checkbox" checked={f.record_live} onChange={(e) => set("record_live", e.target.checked)} /> この放送を自動録画する
            </label>
          </>
        )}
        <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
          <label className="field"><span>開始</span><input type="datetime-local" step="1" value={f.start_at} onChange={(e) => set("start_at", e.target.value)} required /></label>
          <label className="field"><span>終了</span><input type="datetime-local" step="1" value={f.end_at} onChange={(e) => set("end_at", e.target.value)} required /></label>
        </div>
        <label className="field" style={{ display: "block" }}><span>あらすじ</span><textarea value={f.description} onChange={(e) => set("description", e.target.value)} rows={2} style={{ width: "100%" }} /></label>
        <label className="field" style={{ display: "block" }}><span>出演者</span><textarea value={f.cast} onChange={(e) => set("cast", e.target.value)} rows={2} style={{ width: "100%" }} /></label>
        <div style={{ display: "flex", gap: "1rem", alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}><input type="checkbox" checked={f.public_visible} onChange={(e) => set("public_visible", e.target.checked)} /> 公開する</label>
          <label className="field"><span>見逃し公開</span>
            <select value={f.vod_visibility} onChange={(e) => set("vod_visibility", e.target.value)}>
              <option value="off">公開しない</option><option value="public">全員</option><option value="members">会員限定</option><option value="subscribers">サブスク限定</option>
            </select>
          </label>
          <label className="field"><span>配信ポリシー (exposure_policy)</span>
            <select value={f.exposure_policy} onChange={(e) => set("exposure_policy", e.target.value)}>
              {d.exposure_policy_choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>
        </div>
        {f.exposure_policy === "members_yt_site" && (
          <p style={{ border: "1px solid var(--warn)", borderRadius: ".3rem", padding: ".5rem .7rem", fontSize: ".8rem" }}>
            ⚠ 会員限定(YT+サイト)は YouTube メンバー限定ミラー専用の CasparCG チャンネル/エンコードが送出ノードに前提です
            (GPU 容量に専用 GPU が実質必須、members-only 配信の永続 1 本を YouTube Studio で手動作成済みであること)。
            未対応の送出ノードではこのプリセットを選んでも公開 YouTube のフィラー切替のみ有効になります。
          </p>
        )}
        <ClockStyleOverride
          value={f.clock_style_override}
          onChange={(v) => set("clock_style_override", v)}
        />
        <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem" }}>
          <button className="btn" type="submit" disabled={busy}>{programId ? "保存" : "追加"}</button>
          {programId && d.delete_url && <button className="btn" type="button" onClick={del} disabled={busy} style={{ background: "var(--warn)" }}>削除</button>}
        </div>
        </form>
        <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>録画は素材必須・生は live_source 必須 (サーバ検証)。時間重複は保存時に弾かれます。</p>
      </div>
    </StudioPage>
  );
}
