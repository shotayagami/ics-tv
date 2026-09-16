// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";

// Phase 0 の疎通確認スキャフォールド。本番ページへの参照は無い。
export function App() {
  const [health, setHealth] = useState("…");

  useEffect(() => {
    api
      .GET("/api/v1/health/")
      .then(({ data }) =>
        setHealth(data ? `${data.service} ${data.version} (${data.status})` : "unreachable"),
      )
      .catch(() => setHealth("unreachable"));
  }, []);

  return (
    <main style={{ maxWidth: 900, margin: "0 auto", padding: 32 }}>
      <h1>ICS-TV frontend foundation</h1>
      <p>API health: <code>{health}</code></p>
    </main>
  );
}
