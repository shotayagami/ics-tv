// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";

// Bootstrap 公開テーマ束 (移行計画 Phase 0)。@icstv/theme/public.scss を Bootstrap 込みでコンパイルし、
// 安定名 bootstrap-public.css / bootstrap-public.js で出力 → Dockerfile が frontend_dist/bootstrap へ →
// WhiteNoise が /static/web/bootstrap/ で配信 → public_base.html が <link>/<script> でロード。
// SCSS の `@import "bootstrap/scss/..."` は loadPaths(=hoist 済 frontend/node_modules) で解決する。
export default defineConfig({
  base: "/static/web/bootstrap/",
  css: {
    preprocessorOptions: {
      scss: {
        quietDeps: true, // Bootstrap 由来の @import/color 関数 deprecation を抑制
        silenceDeprecations: ["import"], // @icstv/theme 自身の @import (Bootstrap 5.3 が @import 前提) も抑制
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
        entryFileNames: "bootstrap-public.js",
        chunkFileNames: "bootstrap-public-[name].js",
        assetFileNames: "bootstrap-public.[ext]",
      },
    },
  },
});
