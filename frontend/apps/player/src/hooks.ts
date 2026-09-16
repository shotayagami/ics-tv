// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@icstv/api";
import type { components } from "@icstv/api";

export type ChannelDetail = components["schemas"]["ChannelDetail"];
export type ChannelTabData = components["schemas"]["ChannelTab"];
export type CommentItem = components["schemas"]["CommentItem"];
export type CommentListOut = components["schemas"]["CommentListOut"];
export type ScheduleItem = components["schemas"]["ScheduleItem"];
export type ProgramNow = components["schemas"]["ProgramNow"];

/** チャンネル詳細 + タブ一覧を取得し、60s 毎に再取得 (現在番組の繰り上がり追従)。 */
export function useChannelData(slug: string) {
  const [detail, setDetail] = useState<ChannelDetail | null>(null);
  const [tabs, setTabs] = useState<ChannelTabData[]>([]);
  const load = useCallback(async () => {
    const [d, t] = await Promise.all([
      api.GET("/api/v1/channels/{slug}", { params: { path: { slug } } }),
      api.GET("/api/v1/channels"),
    ]);
    if (d.data) setDetail(d.data);
    if (t.data) setTabs(t.data);
  }, [slug]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(id);
  }, [load]);
  return { detail, tabs };
}

/** 毎秒更新の epoch 秒 (進行バー/カウントダウン算出用)。使う側だけが再レンダする。 */
export function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

function readCookie(name: string): string {
  const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/** 視聴ハートビート (#ADMIN-02)。watching() が true の間 ~30s 毎に /api/beat へ POST。 */
export function useHeartbeat(slug: string, watching: () => boolean) {
  const watchingRef = useRef(watching);
  watchingRef.current = watching;
  useEffect(() => {
    if (!slug) return;
    const beat = () => {
      if (!watchingRef.current()) return;
      const body = new FormData();
      body.append("ch", slug);
      void fetch("/api/beat", {
        method: "POST",
        headers: { "X-CSRFToken": readCookie("csrftoken") },
        body,
        credentials: "same-origin",
        keepalive: true,
      }).catch(() => {});
    };
    beat(); // 視聴開始直後に1回
    const id = window.setInterval(beat, 30_000);
    return () => window.clearInterval(id);
  }, [slug]);
}
