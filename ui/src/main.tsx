import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createApiClient } from "@/api/client";
import {
  acquireEntraToken,
  entraSignOut,
  getMsal,
  hasEntraAccount,
} from "@/auth/msal";
import { clearCacheOnSessionChange } from "@/auth/cacheReset";
import { createSession, type EntraAdapter } from "@/auth/session";
import { BootScreen } from "@/components/BootScreen";
import { loadRuntimeConfig, RuntimeConfigError } from "@/config";
import { createAppRouter } from "@/router";
// Inter latin and latin-ext only, from @fontsource-variable/inter (see fonts.css), then the
// audited enterprise token sheet and the global rules, before anything renders (UI-05).
import "@/styles/fonts.css";
import "@/styles/tokens.css";
import "@/styles/global.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html must provide a #root element");
}
const root = createRoot(container);

// D-UI-08-05: nothing renders from the app until /config.json is loaded and valid.
async function start(): Promise<void> {
  root.render(
    <StrictMode>
      <BootScreen state="loading" />
    </StrictMode>,
  );
  try {
    const config = await loadRuntimeConfig();
    // Built once: the router context carries the same client and session to every route.
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    // One user's cached data never reaches the next user in this tab.
    clearCacheOnSessionChange(queryClient);
    const api = createApiClient(config);
    // MSAL is initialized before the router only when the deployment offers Entra sign-in.
    let entra: EntraAdapter | null = null;
    if (config.entraEnabled) {
      await getMsal(config);
      entra = {
        hasAccount: () => hasEntraAccount(config),
        acquire: () => acquireEntraToken(config),
        signOut: () => entraSignOut(config),
      };
    }
    const session = createSession(api, {
      entra,
      navigate: (to, search) => {
        void router.navigate({ to, search });
      },
    });
    const router = createAppRouter({ config, queryClient, api, session });
    root.render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </StrictMode>,
    );
  } catch (error) {
    if (!(error instanceof RuntimeConfigError)) {
      throw error;
    }
    root.render(
      <StrictMode>
        <BootScreen
          state="failed"
          onRetry={() => {
            window.location.reload();
          }}
        />
      </StrictMode>,
    );
  }
}

void start();
