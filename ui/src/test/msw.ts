// One shared msw node server for client and component tests (D-UI-08-30). setup.ts starts it with
// `onUnhandledFrame: "error"` (msw 3 name for onUnhandledRequest) (an unmocked call fails the test), resets the handlers after
// each test and closes it at the end; tests add handlers with `server.use(http.get(...))`.
import { setupServer } from "msw/node";

export { http, HttpResponse } from "msw";
export const server = setupServer();
