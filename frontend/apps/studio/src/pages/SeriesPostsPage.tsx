// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 番組公式サイト 投稿(ブログ/キャンペーン)管理 — SeriesPost CRUD + ファンクラブ限定レベル設定 (#27)
 *
 * URL: /series/:channelSlug/posts/:seriesId (旧スタンドアロンルート互換。ハブは series編集の postsタブ)
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Notice, StudioPage } from "../atoms";

import type { components } from "@icstv/api";

type SeriesPostAdminOut = components["schemas"]["SeriesPostAdminOut"];
type CreatorTierOut = components["schemas"]["CreatorTierOut"];

/** 公開範囲セレクトの選択肢: 完全公開 / 無料会員以上 / 有効な有料ティア(level昇順)。
 * 未定義のレベルは選ばせない (fc_required_level に存在しないレベルを書き込ませない)。 */
function useVisibilityOptions(creatorId: number | null) {
  const [tiers, setTiers] = useState<CreatorTierOut[]>([]);
  useEffect(() => {
    if (!creatorId) {
      setTiers([]);
      return;
    }
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/tiers", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setTiers(((data as CreatorTierOut[]) ?? []).filter((t) => t.is_active)));
  }, [creatorId]);
  return tiers;
}

function PostEditor({
  slug,
  seriesId,
  creatorId,
  post,
  onSave,
  onCancel,
}: {
  slug: string;
  seriesId: string;
  creatorId: number | null;
  post: Partial<SeriesPostAdminOut> | null;
  onSave: () => void;
  onCancel: () => void;
}) {
  const isNew = !post?.id;
  const [kind, setKind] = useState(post?.kind ?? "article");
  const [title, setTitle] = useState(post?.title ?? "");
  const [body, setBody] = useState(post?.body ?? "");
  const [mediaUrl, setMediaUrl] = useState(post?.media_url ?? "");
  const [formUrl, setFormUrl] = useState(post?.form_url ?? "");
  const [campaignStart, setCampaignStart] = useState(post?.campaign_start ?? "");
  const [campaignEnd, setCampaignEnd] = useState(post?.campaign_end ?? "");
  const [isPublished, setIsPublished] = useState(post?.is_published ?? false);
  const [fcLevel, setFcLevel] = useState<string>(
    post?.fc_required_level === null || post?.fc_required_level === undefined ? "" : String(post.fc_required_level)
  );
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const tiers = useVisibilityOptions(creatorId);

  async function save() {
    setSaving(true);
    setErr("");
    const body_ = {
      kind,
      title,
      body,
      media_url: mediaUrl,
      form_url: formUrl,
      campaign_start: campaignStart,
      campaign_end: campaignEnd,
      is_published: isPublished,
      fc_required_level: fcLevel === "" ? null : Number(fcLevel),
    };
    let res: { error?: unknown };
    if (isNew) {
      res = await api.POST("/api/v1/admin/scheduling/{slug}/series/{series_id}/posts", {
        params: { path: { slug, series_id: Number(seriesId) } },
        body: body_,
      });
    } else {
      res = await api.PUT("/api/v1/admin/scheduling/{slug}/series/{series_id}/posts/{post_id}", {
        params: { path: { slug, series_id: Number(seriesId), post_id: post!.id! } },
        body: body_,
      });
    }
    setSaving(false);
    if (res.error) setErr("保存に失敗しました。");
    else onSave();
  }

  const row = (label: string, el: React.ReactNode) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>{label}</label>
      {el}
    </div>
  );
  const inputStyle = { width: "100%", boxSizing: "border-box" as const, background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 };
  const inp = (value: string, onChange: (v: string) => void, placeholder?: string) => (
    <input style={inputStyle} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
  );

  return (
    <div style={{ background: "#0d1530", border: "1px solid #2a3a5a", borderRadius: 12, padding: 20, marginBottom: 20 }}>
      <h3 style={{ fontSize: 14, fontWeight: 800, marginBottom: 16 }}>{isNew ? "投稿を新規作成" : "投稿を編集"}</h3>

      {row(
        "種別",
        <select style={inputStyle} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="article">記事・お知らせ</option>
          <option value="campaign">キャンペーン応募</option>
        </select>
      )}
      {row("タイトル", inp(title, setTitle, "例: 新曲リリースのお知らせ"))}
      {row(
        "本文",
        <textarea style={{ ...inputStyle, resize: "vertical", minHeight: 120 }} value={body} onChange={(e) => setBody(e.target.value)} />
      )}
      {row("画像/動画 URL（任意）", inp(mediaUrl, setMediaUrl, "https://... (R2または外部URL)"))}

      {kind === "campaign" && (
        <>
          {row("応募フォーム URL", inp(formUrl, setFormUrl))}
          <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>受付開始</label>
              <input type="datetime-local" style={inputStyle} value={campaignStart} onChange={(e) => setCampaignStart(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>受付終了</label>
              <input type="datetime-local" style={inputStyle} value={campaignEnd} onChange={(e) => setCampaignEnd(e.target.value)} />
            </div>
          </div>
        </>
      )}

      {row(
        "公開範囲",
        <select style={inputStyle} value={fcLevel} onChange={(e) => setFcLevel(e.target.value)}>
          <option value="">完全公開（誰でも閲覧可）</option>
          {creatorId != null && <option value="0">無料会員以上</option>}
          {tiers.filter((t) => t.level > 0).map((t) => (
            <option key={t.level} value={t.level}>
              有料ティア「{t.name}」(level{t.level}) 以上{!t.joinable ? " — 加入導線は準備中" : ""}
            </option>
          ))}
        </select>
      )}
      {creatorId == null && (
        <div style={{ fontSize: 12, color: "#8899aa", marginTop: -8, marginBottom: 14 }}>
          この番組はクリエイターに紐付けられていないため、常に完全公開になります。
        </div>
      )}

      <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6, marginBottom: 14 }}>
        <input type="checkbox" checked={isPublished} onChange={(e) => setIsPublished(e.target.checked)} />
        公開する
      </label>

      {err && <div style={{ color: "#f87171", fontSize: 13, marginBottom: 10 }}>{err}</div>}
      <div style={{ display: "flex", gap: 10 }}>
        <button type="button" className="btn" onClick={save} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        <button type="button" className="btn secondary" onClick={onCancel}>
          キャンセル
        </button>
      </div>
    </div>
  );
}

/** 投稿管理の本体 (slug/seriesId を props で受ける)。SeriesDetailPage の posts タブ、
 * 旧スタンドアロンルートの両方から再利用する (SeriesFormsPanel と同じ設計)。
 * creatorId は fanclub 紐付け先 (未紐付けは null、公開範囲は完全公開のみ選択可)。 */
export function SeriesPostsPanel({ slug, seriesId, creatorId }: { slug: string; seriesId: string; creatorId: number | null }) {
  const [posts, setPosts] = useState<SeriesPostAdminOut[]>([]);
  const [editing, setEditing] = useState<Partial<SeriesPostAdminOut> | null | false>(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (!slug || !seriesId) return;
    setLoading(true);
    api
      .GET("/api/v1/admin/scheduling/{slug}/series/{series_id}/posts", {
        params: { path: { slug, series_id: Number(seriesId) } },
      })
      .then(({ data, error }) => {
        setLoading(false);
        if (error) setErr("読み込み失敗");
        else setPosts((data as SeriesPostAdminOut[]) ?? []);
      })
      .catch(() => { setLoading(false); setErr("読み込み失敗"); });
  }, [slug, seriesId]);

  useEffect(load, [load]);

  async function deletePost(id: number) {
    if (!window.confirm("この投稿を削除しますか？")) return;
    const { error } = await api.DELETE("/api/v1/admin/scheduling/{slug}/series/{series_id}/posts/{post_id}", {
      params: { path: { slug, series_id: Number(seriesId), post_id: id } },
    });
    if (error) setErr("削除に失敗しました");
    else { setMsg("削除しました"); load(); }
  }

  if (loading) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <>
      {err && <Notice variant="error">{err}</Notice>}
      {msg && <Notice variant="success">{msg}</Notice>}

      {editing !== false && (
        <PostEditor
          slug={slug}
          seriesId={seriesId}
          creatorId={creatorId}
          post={editing}
          onSave={() => { setEditing(false); setMsg("保存しました"); load(); }}
          onCancel={() => setEditing(false)}
        />
      )}

      {!editing && (
        <button type="button" className="btn" style={{ marginBottom: 16 }} onClick={() => setEditing(null)}>
          ＋ 投稿を追加
        </button>
      )}

      {posts.length === 0 && !editing && (
        <div style={{ fontSize: 13, color: "#8899aa", padding: "24px 0" }}>
          投稿がまだありません。「＋ 投稿を追加」から作成してください。
        </div>
      )}

      {posts.map((p) => (
        <div key={p.id} style={{ border: "1px solid #2a2a2a", borderRadius: 10, padding: "14px 16px", marginBottom: 12, background: "#0d1530" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
            <span style={{
              fontSize: 11, fontWeight: 700, borderRadius: 4, padding: "2px 7px",
              background: p.kind === "campaign" ? "rgba(251,191,36,.14)" : "rgba(56,189,248,.14)",
              color: p.kind === "campaign" ? "#fbbf24" : "var(--accent, #38bdf8)",
            }}>
              {p.kind === "campaign" ? "キャンペーン" : "記事・お知らせ"}
            </span>
            <strong style={{ fontSize: 14, flex: 1 }}>{p.title}</strong>
            {p.fc_required_level !== null && (
              <span style={{ fontSize: 11, fontWeight: 700, borderRadius: 4, padding: "2px 7px", background: "rgba(167,139,250,.14)", color: "#a78bfa" }}>
                🔒 {p.fc_required_level === 0 ? "無料会員以上" : `ティア${p.fc_required_level}以上`}
              </span>
            )}
            <span style={{ fontSize: 12, color: p.is_published ? "#4ade80" : "#8899aa" }}>
              {p.is_published ? "● 公開中" : "○ 非公開"}
            </span>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" className="btn" style={{ fontSize: 12, padding: "3px 10px" }} onClick={() => setEditing(p)}>
              編集
            </button>
            <button type="button" className="btn" style={{ fontSize: 12, padding: "3px 10px", background: "#3a1a1a", color: "#f87171" }} onClick={() => void deletePost(p.id)}>
              削除
            </button>
          </div>
        </div>
      ))}
    </>
  );
}

/** 旧スタンドアロンルート /series/:channelSlug/posts/:seriesId (ハブからのリンク先互換)。
 * creatorId は明示的には受け取らないため常に null 扱い(完全公開のみ選択可)。
 * ティア別ゲートを設定したい場合は SeriesDetailPage の posts タブ (creatorId 解決済み) を使う。 */
export function SeriesPostsPage() {
  const { slug = "", seriesId = "" } = useParams();
  return (
    <StudioPage
      title="投稿管理"
      actions={
        <Link to={`/series/${slug}/edit/${seriesId}?tab=posts`} style={{ fontSize: 13, color: "#8899aa", textDecoration: "none" }}>
          ‹ シリーズ編集
        </Link>
      }
    >
      <SeriesPostsPanel slug={slug} seriesId={seriesId} creatorId={null} />
    </StudioPage>
  );
}
