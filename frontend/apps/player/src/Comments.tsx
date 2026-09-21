// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { api } from "@icstv/api";
import { ConfirmDialog, TextField } from "./atoms";

import { type CommentItem, type CommentListOut } from "./hooks";

/** ライブチャット風コメント。閲覧=公開 / 投稿=確認済み会員 / 削除=本人。WS でリアルタイム追記。
 *  投稿/削除は ninja API、リアルタイム受信は /ws/comments/<slug>/ (構造化ペイロード)。 */
export function Comments({ slug, homeBase }: { slug: string; homeBase: string }) {
  const [meta, setMeta] = useState<CommentListOut | null>(null);
  const [items, setItems] = useState<CommentItem[]>([]);
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const seen = useRef<Set<number>>(new Set());

  const addItem = useCallback((it: CommentItem, prepend: boolean) => {
    if (seen.current.has(it.id)) return; // 既出(自分のAJAX描画/WS二重)を弾く
    seen.current.add(it.id);
    setItems((prev) => (prepend ? [it, ...prev] : [...prev, it]));
  }, []);

  // 初期一覧 (新しい順) + ゲート状態
  useEffect(() => {
    let alive = true;
    void api
      .GET("/api/v1/channels/{slug}/comments", { params: { path: { slug } } })
      .then(({ data }) => {
        if (!alive || !data) return;
        setMeta(data);
        data.items.forEach((it) => addItem(it, false));
      });
    return () => {
      alive = false;
    };
  }, [slug, addItem]);

  // WebSocket 受信 (#COMM-01)。切断時は自動再接続。
  useEffect(() => {
    if (!slug || !window.WebSocket) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer = 0;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const connect = () => {
      try {
        ws = new WebSocket(`${proto}//${location.host}/ws/comments/${slug}/`);
      } catch {
        return;
      }
      ws.onmessage = (ev) => {
        let d: { type?: string; comment?: CommentItem };
        try {
          d = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (d.type === "comment.new" && d.comment) addItem(d.comment, true);
      };
      ws.onclose = () => {
        if (!closed) timer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [slug, addItem]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const text = body.trim();
    if (!text) return;
    const { data, error: err, response } = await api.POST("/api/v1/channels/{slug}/comments", {
      params: { path: { slug } },
      body: { body: text },
    });
    if (response.ok && data) {
      addItem(data, true);
      setBody("");
      setError(null);
    } else {
      setError((err as { detail?: string } | undefined)?.detail ?? "投稿に失敗しました。");
    }
  }

  async function del(id: number) {
    const { response } = await api.DELETE("/api/v1/channels/{slug}/comments/{cid}", {
      params: { path: { slug, cid: id } },
    });
    if (response.ok) {
      seen.current.delete(id);
      setItems((prev) => prev.filter((c) => c.id !== id));
    }
  }

  const gate = meta?.gate ?? "login";
  const me = meta?.me_member_id ?? null;

  return (
    <section className="comments" id="comments">
      <h2 className="comments-h">
        コメント <span className="muted">({items.length})</span>
      </h2>

      {gate === "ok" ? (
        <form className="comment-form" onSubmit={submit}>
          <TextField
            multiline
            value={body}
            onChange={setBody}
            rows={2}
            maxLength={500}
            required
            ariaLabel="コメント"
            placeholder={`コメントを入力（${meta?.nickname ?? ""}）`}
            error={error ?? undefined}
          />
          <button type="submit">送信</button>
        </form>
      ) : gate === "verify" ? (
        <div className="comment-gate">
          コメントするには本人確認が必要です。
          <a href={`${homeBase}/members/verify-required/`}>本人確認へ ›</a>
        </div>
      ) : (
        <div className="comment-gate">
          コメントするには <a href={`${homeBase}/members/login/?next=/ch/${slug}/`}>ログイン</a> または{" "}
          <a href={`${homeBase}/members/register/`}>会員登録</a> が必要です。
        </div>
      )}

      <div className="comments-list">
        {items.length === 0 ? (
          <div className="comments-empty">まだコメントはありません。</div>
        ) : (
          items.map((c) => (
            <div className="comment" key={c.id} data-cid={c.id}>
              <div className="c-head">
                <span className="c-nick">{c.nickname}</span>
                {c.badge && (
                  <span className="c-badge" title="コメント特典会員">
                    ★会員
                  </span>
                )}
                <span className="c-time tabnum">{c.time}</span>
                {c.program_title && <span className="c-prog">{c.program_title}</span>}
                {me !== null && c.member_id === me && (
                  <span className="c-del">
                    <button type="button" aria-label="削除" onClick={() => setConfirmId(c.id)}>
                      ×
                    </button>
                  </span>
                )}
              </div>
              <div className="c-body">{c.body}</div>
            </div>
          ))
        )}
      </div>

      <ConfirmDialog
        open={confirmId !== null}
        title="コメントを削除"
        message="このコメントを削除しますか？元に戻せません。"
        destructive
        confirmLabel="削除する"
        onConfirm={() => {
          if (confirmId !== null) void del(confirmId);
          setConfirmId(null);
        }}
        onCancel={() => setConfirmId(null)}
      />
    </section>
  );
}
