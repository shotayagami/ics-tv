// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";

// Bootstrap 管理テーマ束 (移行計画 Phase 0)。@icstv/theme/admin.scss を Bootstrap 込みでコンパイルし、
// 安定名 bootstrap-admin.css / bootstrap-admin.js で出力 → Dockerfile が frontend_dist/bootstrap へ →
// WhiteNoise が /static/web/bootstrap/ で配信。studio/ops/旧admin の SSR シェルがロードする。
export default defineConfig({
  base: "/static/web/bootstrap/",
  css: {
    preprocessorOptions: {
      scss: {
        quietDeps: true,
        silenceDeprecations: ["import"], // Bootstrap 5.3 が @import 前提のため抑制
        loadPaths: [fileURLToPath(new URL("../../node_modules", import.meta.url))],
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.ts", import.meta.url)),
      output: {
        entryFileNames: "bootstrap-admin.js",
        chunkFileNames: "bootstrap-admin-[name].js",
        assetFileNames: "bootstrap-admin.[ext]",
      },
    },
  },
});
