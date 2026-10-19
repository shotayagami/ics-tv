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

export type ChannelLoadError = { kind: "auth" | "forbidden" | "request" | "network"; status?: number };
export type ChannelRefreshResult = "updated" | "denied" | "failed";

/** チャンネル詳細 + タブ一覧を取得し、60s 毎に再取得 (現在番組の繰り上がり追従)。 */
export function useChannelData(slug: string) {
  const [detail, setDetail] = useState<ChannelDetail | null>(null);
  const [tabs, setTabs] = useState<ChannelTabData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ChannelLoadError | null>(null);
  const [empty, setEmpty] = useState(false);
  const load = useCallback(async (showLoading = true): Promise<ChannelRefreshResult> => {
    if (showLoading) setLoading(true);
    setError(null);
    setEmpty(false);
    try {
      const d = await api.GET("/api/v1/channels/{slug}", { params: { path: { slug } } });
      if (d.error) {
        if (d.response.status === 404) setEmpty(true);
        else setError({ kind: d.response.status === 401 ? "auth" : d.response.status === 403 ? "forbidden" : "request", status: d.response.status });
        return d.response.status === 401 || d.response.status === 403 || d.response.status === 404 ? "denied" : "failed";
      }
      if (!d.data) {
        setEmpty(true);
        return "denied";
      }
      setDetail(d.data);
      try {
        const t = await api.GET("/api/v1/channels");
        if (t.data) setTabs(t.data);
        else if (t.error) setError({ kind: t.response.status === 401 ? "auth" : t.response.status === 403 ? "forbidden" : "request", status: t.response.status });
      } catch {
        setError({ kind: "network" });
      }
      return d.data.hls_url ? "updated" : "denied";
    } catch {
      setError({ kind: "network" });
      return "failed";
    } finally {
      setLoading(false);
    }
  }, [slug]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(false), 60_000);
    return () => window.clearInterval(id);
  }, [load]);
  const retry = useCallback(() => load(), [load]);
  const refreshHlsUrl = useCallback(() => load(false), [load]);
  return { detail, tabs, loading, error, empty, retry, refreshHlsUrl };
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
