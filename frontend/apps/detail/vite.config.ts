// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 公開「詳細ページ」島 (#Phase2c)。番組詳細の操作バー (program.html) と 見逃し録画プレイヤー
// (vod_detail.html) を data-mode で分ける単一バンドル。スタイルは各ページの SSR <style> を
// 流用するため CSS は持たない。安定名 detail.js → /static/web/detail/。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/detail/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "detail.js",
        chunkFileNames: "detail-[name].js",
        assetFileNames: "detail.[ext]",
      },
    },
  },
});
