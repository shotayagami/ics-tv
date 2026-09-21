// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState } from "./atoms";

import { OpsConsole } from "./pages/OpsConsole";
import { Timekeeper } from "./pages/Timekeeper";

/** ops.* ルート (/) は先頭チャンネルの放送コンソールへリダイレクト。 */
function ChannelRedirect() {
  const [slug, setSlug] = useState<string | null>(null);
  const [empty, setEmpty] = useState(false);
  useEffect(() => {
    api
      .GET("/api/v1/admin/scheduling/channels")
      .then(({ data }) => (data && data.length ? setSlug(data[0].slug) : setEmpty(true)))
      .catch(() => setEmpty(true));
  }, []);
  if (empty) return <EmptyState loading>チャンネルがありません。</EmptyState>;
  if (!slug) return <EmptyState loading>読み込み中…</EmptyState>;
  return <Navigate to={`/${slug}`} replace />;
}

/** 🔴 放送コンソール SPA (リファクタ Phase 1)。ops.* 専用ホストにモバイルファーストで配信。
 * Phase 1.1 は読み取り専用 (状態監視)。送出操作は P1.2 で追加。 */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<ChannelRedirect />} />
      <Route path="/:slug" element={<OpsConsole />} />
      <Route path="/:slug/timekeeper" element={<Timekeeper />} />
      <Route path="*" element={<ChannelRedirect />} />
    </Routes>
  );
}
