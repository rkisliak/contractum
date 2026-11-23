import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// D-UI-08-05, D-UI-08-09: in `pnpm dev` /config.json is answered from ui/dev/config.json.
// The plugin is serve-only and nothing lives in public/, so no config.json ships in dist;
// in production the nginx entrypoint writes it at container start.
function devRuntimeConfig(): Plugin {
  const file = resolve(import.meta.dirname, "dev/config.json");
  return {
    name: "dc-dev-runtime-config",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/config.json", (request, response, next) => {
        if (request.method !== "GET" && request.method !== "HEAD") {
          next();
          return;
        }
        readFile(file, "utf-8").then(
          (body) => {
            response.setHeader("Content-Type", "application/json");
            response.setHeader("Cache-Control", "no-store");
            response.end(request.method === "HEAD" ? undefined : body);
          },
          (error: unknown) => {
            next(error);
          },
        );
      });
    },
  };
}

// The image's build identity for the "Versions" dialog (src/buildInfo.ts): the GIT_SHA and
// BUILD_TIME build arguments of infra/docker/ui.Dockerfile ("dev" locally). The platform version is
// not part of the bundle: it comes from config.json at runtime (DC_UI_VERSION), so one build can be
// tested and then released unchanged.
export default defineConfig({
  define: {
    __DC_BUILD__: JSON.stringify({
      commit: process.env.GIT_SHA || "dev",
      builtAt: process.env.BUILD_TIME || "dev",
    }),
  },
  plugins: [
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    devRuntimeConfig(),
  ],
  resolve: {
    alias: { "@": resolve(import.meta.dirname, "src") },
  },
  build: {
    // Vite 8 bundles with Rolldown; `rollupOptions` is only an alias, and setting both drops it.
    rolldownOptions: {
      // The MSAL v5 redirect bridge is a second page (A-4).
      input: {
        main: resolve(import.meta.dirname, "index.html"),
        redirect: resolve(import.meta.dirname, "redirect.html"),
      },
      output: {
        // A cold load must stay well inside the gateway's per-client burst (rl-ui, 600):
        // route-level splitting alone made "/" fetch over 40 files. Libraries and the shared
        // components load as two files; Monaco and MSAL stay lazy, in their own chunks.
        codeSplitting: {
          groups: [
            {
              name: "vendor",
              test: (id: string) =>
                id.includes("/node_modules/") &&
                !id.includes("monaco") &&
                !id.includes("@azure/msal") &&
                !id.includes("yaml") &&
                !id.includes("jsonc-parser"),
            },
            { name: "components", test: /\/src\/(components|lib|api)\// },
          ],
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    // D-UI-08-09: the dev server forwards /api to the compose gateway. The
    // gateway certificate is self-signed in development, hence secure: false.
    proxy: { "/api": { target: "https://localhost:8443", secure: false } },
  },
});
