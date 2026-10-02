import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig(() => {
  const apiPort = Number.parseInt(process.env.YXY_API_PORT || "8765", 10);
  return {
    plugins: [react()],
    clearScreen: false,
    build: {
      rollupOptions: {
        input: {
          main: fileURLToPath(new URL("./index.html", import.meta.url)),
          ai: fileURLToPath(new URL("./ai.html", import.meta.url)),
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 1420,
      strictPort: true,
      proxy: { "/api": `http://127.0.0.1:${apiPort}` },
    },
  };
});
