// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 公開ディスカバリ「島」(#Phase2c)。search.html / browse.html / vod.html に data-mode で
// マウントする単一バンドル (hls 不要)。安定名 discover.js/discover.css → /static/web/discover/。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/discover/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "discover.js",
        chunkFileNames: "discover-[name].js",
        assetFileNames: "discover.[ext]",
      },
    },
  },
});
