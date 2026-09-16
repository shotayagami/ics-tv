// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import type { components } from "@icstv/api";

export type HomeCard = components["schemas"]["HomeCard"];
export type HomeOut = components["schemas"]["HomeOut"];

/** /api/v1/home を初回取得 + 12s 毎ポーリング (リロード無し更新)。タブ非表示中は休止。 */
export function useHomeData(): HomeOut | null {
  const [data, setData] = useState<HomeOut | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => {
      api
        .GET("/api/v1/home")
        .then(({ data }) => {
          if (alive && data) setData(data);
        })
        .catch(() => {});
    };
    load();
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
  }, []);
  return data;
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
