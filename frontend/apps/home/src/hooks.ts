// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import type { components } from "@icstv/api";

export type HomeCard = components["schemas"]["HomeCard"];
export type HomeOut = components["schemas"]["HomeOut"];

/** /api/v1/home を初回取得 + 12s 毎ポーリング (リロード無し更新)。タブ非表示中は休止。 */
export function useHomeData(): { data: HomeOut | null; error: boolean; retry: () => void } {
  const [data, setData] = useState<HomeOut | null>(null);
  const [error, setError] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const result = await api.GET("/api/v1/home");
        if (!alive) return;
        if (result.error) setError(true);
        else if (result.data) {
          setData(result.data);
          setError(false);
        }
      } catch {
        if (alive) setError(true);
      }
    };
    void load();
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, 12_000);
    const onVis = () => {
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [retryCount]);
  return { data, error, retry: () => setRetryCount((n) => n + 1) };
}

/** 毎秒更新の epoch 秒。進行バー/カウントダウンの独立 leaf でだけ使い、カード/動画は再レンダさせない。 */
export function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}
