// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 🔴 放送コンソール SPA (リファクタ Phase 1)。studio (#Phase2d) と同じ Option B:
// Django が ops.* ホストで ops/app.html シェル (#ops-root + ops.js) を staff 限定で返すだけ。
// モバイルファースト (ワンオペ外出先即応)。安定名 ops.js/ops.css → /static/web/ops/。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/ops/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "ops.js",
        chunkFileNames: "ops-[name].js",
        assetFileNames: "ops.[ext]",
      },
    },
  },
});
