// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 公開トップ ライブカード「島」(#Phase2b)。home.html の hero+チャンネルカードにマウントする
// 単一バンドル (hls.js 同梱)。安定名 home.js/home.css。css は SSR セクション (これからの番組/VOD/
// ジャンル) のスタイルも含むため Django ページが <link> で読み込む (/static/web/home/home.css)。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/home/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "home.js",
        chunkFileNames: "home-[name].js",
        assetFileNames: "home.[ext]",
      },
    },
  },
});
