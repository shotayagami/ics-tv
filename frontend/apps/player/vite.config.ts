// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 公開プレイヤー「島」(#Phase1)。Django の epg.html に <script type=module> でマウントする
// 単一バンドル。安定ファイル名 player.js / player.css で出力し、Dockerfile が
// frontend_dist/player へ配置 → WhiteNoise が /static/web/player/ で配信する。
// hls.js を静的 import で同梱 (動的 import の chunk を作らない = 単一ファイル)。
// series-form.js は番組ページのフォーム島 (series_detail.html)。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/player/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        player: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
        "series-form": fileURLToPath(new URL("./src/series-form.tsx", import.meta.url)),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name]-[hash].js",
        assetFileNames: "[name].[ext]",
      },
    },
  },
});
