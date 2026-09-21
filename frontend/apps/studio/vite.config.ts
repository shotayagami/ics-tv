// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// studio 管理 SPA (#Phase2d)。公開の「島」と違い真の SPA (React Router)。Django は
// studio/app.html シェル (#studio-root + studio.js) を staff 限定で返すだけ。安定名
// studio.js/studio.css → /static/web/studio/ (Option B: collectstatic + WhiteNoise 配信)。
export default defineConfig({
  plugins: [react()],
  base: "/static/web/studio/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "studio.js",
        chunkFileNames: "studio-[name].js",
        assetFileNames: "studio.[ext]",
      },
    },
  },
});
