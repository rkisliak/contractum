// The MSAL v5 redirect bridge (amendment A-4): the IdP returns here (/redirect.html, a second
// Vite entry with no inline script). For a redirect sign-in the bridge caches the response and
// navigates back to the page that started it (/auth/callback); for silent renewal in a hidden
// iframe it hands the response to the main frame. nginx serves this page with
// `frame-ancestors 'self'` and no COOP (A-7).
import { broadcastResponseToMainFrame } from "@azure/msal-browser/redirect-bridge";

broadcastResponseToMainFrame().catch((error: unknown) => {
  console.error(error);
});
