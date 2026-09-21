// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Option B: Django(WhiteNoise) が /static/web/ から配信する。collectstatic が
// STATICFILES_DIRS [("web", frontend_dist)] を STATIC_ROOT/web へ集約するため、
// アセット URL の base を /static/web/ に固定する。SPA シェル html は /app/ で配信。
// 将来フロント Pod を分離する場合のみ base/配信方法を見直す (移行計画 Phase 3 以降)。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
