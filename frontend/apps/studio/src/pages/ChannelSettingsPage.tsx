// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Notice, OldScreenLink, StatusBadge, StudioPage, SubNav } from "../atoms";

import type { ChannelSettingsOut } from "../hooks";
import { postFile, postForm } from "../hooks";
import { RouterLink } from "../links";

/** チャンネル詳細設定 (#Phase2e-4)。基本操作 = 既定フィラー/スレートの割当・CF 再生 URL を SPA で完結。
 * YouTube OAuth 接続はサーバ描画フローへ <a> リンク (JWT 化しない)、CF/YT の重い外部 API 作成は
 * 旧詳細画面 (advanced_url) に据え置く。既存 set_channel_media / set_cf_playback_url を form-POST 再利用。 */
export function ChannelSettingsPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<ChannelSettingsOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const [filler, setFiller] = useState("");
  const [slate, setSlate] = useState("");
  const [siteOnlyFiller, setSiteOnlyFiller] = useState("");
  const [membersFiller, setMembersFiller] = useState("");
  const [hls, setHls] = useState("");
  const [chimeName, setChimeName] = useState("");
  const [chimeFile, setChimeFile] = useState<File | null>(null);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/channels/{slug}/settings", { params: { path: { slug } } })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗 (staff 権限が必要)");
        setD(data);
        setFiller(data.default_filler_id ? String(data.default_filler_id) : "");
        setSlate(data.slate_asset_id ? String(data.slate_asset_id) : "");
        setSiteOnlyFiller(data.site_only_filler_id ? String(data.site_only_filler_id) : "");
        setMembersFiller(data.members_filler_id ? String(data.members_filler_id) : "");
        setHls(data.cloudflare.playback_hls_url);
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug]);
  useEffect(load, [load]);

  async function op(url: string, fields: Record<string, string>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status, data } = await postForm(url, fields);
    setBusy(false);
    if (status === 200 || status === 204) {
      setMsg(okMsg);
      load();
    } else setErr(typeof data === "string" ? data : "保存に失敗しました");
  }

  async function uploadSound() {
    if (!chimeFile || busy || !d) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status, data } = await postFile(d.chime_sound_post_url, { name: chimeName, file: chimeFile });
    setBusy(false);
    if (status === 200) {
      setMsg("音源を追加しました");
      setChimeName("");
      setChimeFile(null);
      load();
    } else setErr(typeof data === "string" && data ? data : "アップロードに失敗しました");
  }

  async function deleteSound(id: number) {
    if (busy) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status } = await postForm(`/admin-ui/chime/sound/${id}/delete/`, {});
    setBusy(false);
    if (status === 200) {
      setMsg("音源を削除しました");
      load();
    } else setErr("削除に失敗しました");
  }

  async function selectChime(category: string, sound: string) {
    if (busy || !d) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const { status } = await postForm(d.chime_select_url, { category, sound });
    setBusy(false);
    if (status === 200) {
      setMsg("チャイムを設定しました");
      load();
    } else setErr("設定に失敗しました");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;
  const yt = d.youtube;
  const cf = d.cloudflare;

  return (
    <StudioPage
      title={`${d.name} の詳細設定`}
      breadcrumb={
        <Breadcrumb items={[{ label: "チャンネル管理", href: "/channels" }]} linkComponent={RouterLink} />
      }
      actions={<span className="muted tabnum">{d.slug}</span>}
    >
      <SubNav
        items={[
          { label: "詳細設定", href: `/channels/${slug}/settings`, active: true },
          { label: "時計エディタ", href: `/channels/${slug}/clock`, active: false },
        ]}
        linkComponent={RouterLink}
      />

      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <div className="st-stack">
        {/* 既定フィラー / スレート (基本操作 = SPA 完結) */}
        <div className="card">
          <h3 style={{ margin: "0 0 .5rem" }}>既定メディア</h3>
          <label style={{ display: "block", marginBottom: ".5rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>隙間充填フィラー</span>
            <select value={filler} onChange={(e) => setFiller(e.target.value)} style={{ display: "block", width: "100%", marginTop: ".2rem" }}>
              <option value="">（未設定）</option>
              {d.filler_options.map((f) => <option key={f.id} value={String(f.id)}>{f.name}</option>)}
            </select>
          </label>
          <label style={{ display: "block", marginBottom: ".6rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>緊急スレート素材 (正規化済のみ)</span>
            <select value={slate} onChange={(e) => setSlate(e.target.value)} style={{ display: "block", width: "100%", marginTop: ".2rem" }}>
              <option value="">（未設定）</option>
              {d.slate_options.map((a) => <option key={a.id} value={String(a.id)}>{a.name}</option>)}
            </select>
          </label>
          <label style={{ display: "block", marginBottom: ".6rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>exposure_policy 公開ミラー案内フィラー (正規化済のみ)</span>
            <select value={siteOnlyFiller} onChange={(e) => setSiteOnlyFiller(e.target.value)} style={{ display: "block", width: "100%", marginTop: ".2rem" }}>
              <option value="">（未設定）</option>
              {d.slate_options.map((a) => <option key={a.id} value={String(a.id)}>{a.name}</option>)}
            </select>
          </label>
          <label style={{ display: "block", marginBottom: ".6rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>exposure_policy メンバーミラー待機画 (正規化済のみ)</span>
            <select value={membersFiller} onChange={(e) => setMembersFiller(e.target.value)} style={{ display: "block", width: "100%", marginTop: ".2rem" }}>
              <option value="">（未設定）</option>
              {d.slate_options.map((a) => <option key={a.id} value={String(a.id)}>{a.name}</option>)}
            </select>
          </label>
          <button className="btn" type="button" disabled={busy} onClick={() => op(d.media_post_url, { default_filler: filler, slate_asset: slate, site_only_filler: siteOnlyFiller, members_filler: membersFiller }, "既定メディアを保存しました")}>保存</button>
          <p style={{ margin: ".7rem 0 0", fontSize: ".8rem" }}>
            <RouterLink href={`/graphic-cues/${slug}/filler/0`} className="muted">フィラー基本グラフィック(CG) を編集 ›</RouterLink>
          </p>
          <span className="muted" style={{ fontSize: ".72rem" }}>フィラー再生中の全クリップに常時適用される基本 CG セット。</span>
        </div>

        {/* 速報チャイム (layer41・速報テロップと同時発火する効果音) */}
        <div className="card">
          <h3 style={{ margin: "0 0 .5rem" }}>速報チャイム</h3>
          <p className="muted" style={{ fontSize: ".78rem", margin: "0 0 .6rem" }}>
            速報テロップと同時に鳴らす効果音。音源をライブラリに登録し、カテゴリ別に選びます。MP3 / WAV / M4A / AAC / OGG / WebM・5MB 以内。
          </p>

          {/* カテゴリ別の選択 */}
          {d.chimes.map((c) => (
            <label key={c.category} style={{ display: "block", marginBottom: ".4rem" }}>
              <span className="muted" style={{ fontSize: ".8rem" }}>{c.label}</span>
              <select
                value={c.selected_sound_id ? String(c.selected_sound_id) : ""}
                onChange={(e) => selectChime(c.category, e.target.value)}
                disabled={busy}
                style={{ display: "block", width: "100%", marginTop: ".2rem" }}
              >
                <option value="">（既定音・未選択）</option>
                {d.chime_library.map((s) => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
              </select>
            </label>
          ))}

          {/* ライブラリ一覧 */}
          <div style={{ borderTop: "1px solid var(--line)", marginTop: ".6rem", paddingTop: ".5rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>音源ライブラリ</span>
            {d.chime_library.length === 0 && (
              <p className="muted" style={{ fontSize: ".75rem", margin: ".3rem 0" }}>音源がありません。下から追加してください。</p>
            )}
            {d.chime_library.map((s) => (
              <div key={s.id} style={{ display: "flex", gap: ".4rem", alignItems: "center", margin: ".25rem 0" }}>
                <span style={{ fontSize: ".8rem", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={s.filename}>{s.name}</span>
                <audio controls preload="none" src={s.preview_url} style={{ height: 28, maxWidth: 160 }} />
                <button className="btn ghost" type="button" disabled={busy} onClick={() => deleteSound(s.id)}>削除</button>
              </div>
            ))}
          </div>

          {/* 音源を追加 */}
          <div style={{ borderTop: "1px solid var(--line)", marginTop: ".5rem", paddingTop: ".5rem", display: "flex", gap: ".4rem", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              value={chimeName}
              onChange={(e) => setChimeName(e.target.value)}
              placeholder="名称 (任意)"
              style={{ fontSize: ".8rem", flex: "1 1 120px", minWidth: 0 }}
            />
            <input
              type="file"
              accept="audio/*"
              onChange={(e) => setChimeFile(e.target.files?.[0] ?? null)}
              style={{ fontSize: ".75rem", flex: "1 1 140px", minWidth: 0 }}
            />
            <button className="btn" type="button" disabled={busy || !chimeFile} onClick={() => uploadSound()}>追加</button>
          </div>
        </div>

        {/* Cloudflare Live */}
        <div className="card">
          <h3 style={{ margin: "0 0 .5rem" }}>Cloudflare Live</h3>
          <p className="muted" style={{ fontSize: ".8rem", margin: "0 0 .4rem" }}>
            Live Input: {cf.live_input_id ? <span className="tabnum" style={{ color: "var(--fg)" }}>{cf.live_input_id}</span> : <span style={{ color: "var(--warn)" }}>未作成</span>}
          </p>
          <label style={{ display: "block", marginBottom: ".5rem" }}>
            <span className="muted" style={{ fontSize: ".8rem" }}>視聴者向け HLS 再生 URL</span>
            <input value={hls} onChange={(e) => setHls(e.target.value)} placeholder="https://…/manifest/video.m3u8" style={{ display: "block", width: "100%", marginTop: ".2rem" }} />
          </label>
          <button className="btn" type="button" disabled={busy} onClick={() => op(cf.set_playback_url, { cf_playback_hls_url: hls }, "再生 URL を保存しました")}>保存</button>
        </div>

        {/* YouTube 連携 (OAuth はサーバ描画フロー) */}
        <div className="card">
          <h3 style={{ margin: "0 0 .5rem" }}>YouTube 連携</h3>
          <p style={{ margin: "0 0 .4rem", fontSize: ".85rem" }}>
            {yt.connected ? <StatusBadge label="接続済 (書き込み可)" tone="ok" /> : <StatusBadge label="未接続" tone="warn" />}
            {yt.has_config ? <span className="muted" style={{ marginLeft: ".5rem" }}>枠テンプレ設定済</span> : <span className="muted" style={{ marginLeft: ".5rem" }}>枠テンプレ未設定</span>}
          </p>
          {yt.oauth_configured ? (
            <a className="btn" href={yt.connect_url}>{yt.connected ? "再接続 (OAuth)" : "アカウント接続 (OAuth)"}</a>
          ) : (
            <p className="muted" style={{ fontSize: ".78rem" }}>サーバに OAuth クライアント (ICSTV_OAUTH_CLIENT_ID/SECRET) が未設定です。</p>
          )}
        </div>
      </div>

      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".7rem" }}>
        Live Input の作成・YouTube ライブ配信枠の生成・枠テンプレ編集など外部 API を伴う重い操作は{" "}
        <OldScreenLink href={d.advanced_url}>詳細設定</OldScreenLink> で行います。OAuth 接続はサーバ側フローで完結します。
      </p>
    </StudioPage>
  );
}
