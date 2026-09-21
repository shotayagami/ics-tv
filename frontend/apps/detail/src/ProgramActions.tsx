// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import { Button, DateTimePill } from "./atoms";
import { useToast } from "./toast";

import type { ProgramDetailOut } from "./hooks";
import { postToggle } from "./hooks";

/** 共有ボタン (X intent fallback + navigator.share への進化的拡張)。SSR の _share.html と同等。 */
function ShareButton({ url, title }: { url: string; title: string }) {
  const href = `https://twitter.com/intent/tweet?text=${encodeURIComponent(title)}&url=${encodeURIComponent(url)}`;
  function onClick(e: React.MouseEvent) {
    if (navigator.share) {
      e.preventDefault();
      navigator.share({ title, url }).catch(() => {});
    }
  }
  return (
    <a className="btn-share" target="_blank" rel="noopener" href={href} onClick={onClick} aria-label="共有する">
      <svg width="15" height="15" viewBox="0 0 18 18" aria-hidden="true">
        <circle cx="14" cy="4" r="2.2" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="4" cy="9" r="2.2" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="14" cy="14" r="2.2" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M6 8 L12 5 M6 10 L12 13" fill="none" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      共有
    </a>
  );
}

/** 番組詳細の操作バー: 状態別CTA + お気に入り/リマインド (会員) + 共有。
 *
 * 初期状態は /api/v1/program/{id} から取得。トグルは既存 member AJAX エンドポイントへ
 * 楽観更新で POST する (失敗時はロールバック)。未ログイン/未認証はログイン/認証導線へ。 */
export function ProgramActions({ programId, base }: { programId: number; base: string }) {
  const [d, setD] = useState<ProgramDetailOut | null>(null);
  const [fav, setFav] = useState(false);
  const [rem, setRem] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    let alive = true;
    api
      .GET("/api/v1/program/{program_id}", { params: { path: { program_id: programId } } })
      .then(({ data }) => {
        if (!alive || !data) return;
        setD(data);
        setFav(data.is_favorited);
        setRem(data.is_reminded);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [programId]);

  if (!d) return null;

  const loginHref = `${base}/members/login/?next=/program/${programId}/`;

  async function toggleFav() {
    if (busy) return;
    const next = !fav;
    setFav(next); // 楽観
    setBusy(true);
    try {
      const r = await postToggle(`${base}/members/favorites/${programId}/toggle/`);
      setFav(Boolean(r.favorited));
    } catch {
      setFav(!next); // ロールバック
      toast.error("お気に入りの更新に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  async function toggleRem() {
    if (busy) return;
    const next = !rem;
    setRem(next);
    setBusy(true);
    try {
      const r = await postToggle(`${base}/members/reminders/${programId}/toggle/`);
      setRem(Boolean(r.reminded));
    } catch {
      setRem(!next);
      toast.error("通知設定の更新に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  // 状態別 CTA (SSR と同じ優先順位)。yt は外部リンク (別タブ)、それ以外は base 付き内部リンク。
  let cta = null;
  if (d.cta.kind === "live" || d.cta.kind === "vod") {
    cta = (
      <Button variant="play" href={`${base}${d.cta.url}`}>
        {d.cta.label}
      </Button>
    );
  } else if (d.cta.kind === "yt") {
    cta = (
      <Button variant="play" href={d.cta.url} target="_blank" rel="noopener">
        {d.cta.label}
      </Button>
    );
  }

  // リマインド (upcoming のみ)。会員かつ認証済 → トグル、会員未認証 → 認証導線、未ログイン → ログイン。
  let reminder = null;
  if (d.state === "upcoming") {
    if (d.is_member && d.can_remind) {
      reminder = (
        <Button variant="pin" active={rem} onClick={toggleRem} disabled={busy}>
          {rem ? "🔔 通知ON" : "🔔 開始を通知"}
        </Button>
      );
    } else if (d.is_member) {
      reminder = (
        <Button variant="pin" href={`${base}/members/verify-required/`}>
          🔔 開始を通知
        </Button>
      );
    } else {
      reminder = (
        <Button variant="pin" href={loginHref}>
          🔔 開始を通知
        </Button>
      );
    }
  }

  // お気に入り (全状態)。会員 → トグル、未ログイン → ログイン導線。
  const favorite = d.is_member ? (
    <Button variant="pin" active={fav} onClick={toggleFav} disabled={busy}>
      {fav ? "★ マイリスト済" : "☆ あとで見る"}
    </Button>
  ) : (
    <Button variant="pin" href={loginHref}>
      ☆ あとで見る
    </Button>
  );

  return (
    <>
      {d.state === "upcoming" && <DateTimePill>{d.start_pill}</DateTimePill>}
      {cta}
      {reminder}
      {favorite}
      <ShareButton url={d.share_url} title={d.share_title} />
    </>
  );
}
