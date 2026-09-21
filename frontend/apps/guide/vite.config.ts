// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 公開番組表「島」(#Phase2)。Django の guide.html に <script type=module> でマウントする
// 単一バンドル。安定名 guide.js / guide.css で出力 → Dockerfile が frontend_dist/guide へ配置 →
// WhiteNoise が /static/web/guide/ で配信。:root トークンは public_base が供給。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/guide/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "guide.js",
        chunkFileNames: "guide-[name].js",
        assetFileNames: "guide.[ext]",
      },
    },
  },
});
