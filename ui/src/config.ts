// Runtime configuration (D-UI-08-05). The nginx entrypoint writes /config.json from
// DC_UI_* environment variables at container start; `pnpm dev` serves ui/dev/config.json
// through a Vite middleware instead. Only public identifiers ever appear in it.

export type RuntimeConfig = {
  entraEnabled: boolean;
  entraClientId: string;
  entraAuthority: string;
  entraScopes: string[];
  entraProtocol: "aad" | "oidc";
  localLoginEnabled: boolean;
  apiBase: string;
  // The platform version, written by the entrypoint from DC_UI_VERSION. Optional so a config
  // built in a test needs no version; parseRuntimeConfig always sets it ("dev" when absent).
  version?: string;
};

// The same pattern the nginx entrypoint enforces on DC_UI_VERSION before it writes the file.
const VERSION_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}$/;

export class RuntimeConfigError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "RuntimeConfigError";
  }
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  if (typeof value !== "string") {
    throw new RuntimeConfigError(`config.json key "${key}" must be a string`);
  }
  return value;
}

function readBoolean(body: Record<string, unknown>, key: string): boolean {
  const value = body[key];
  if (typeof value !== "boolean") {
    throw new RuntimeConfigError(`config.json key "${key}" must be a boolean`);
  }
  return value;
}

// T-8-04: every key and type is validated, and the API base must be a same-origin
// path, because the CSP only allows connect-src 'self' plus the Entra authority.
function parseRuntimeConfig(body: unknown): RuntimeConfig {
  if (!isRecord(body)) {
    throw new RuntimeConfigError("config.json must be a JSON object");
  }
  const protocol = readString(body, "entraProtocol");
  if (protocol !== "aad" && protocol !== "oidc") {
    throw new RuntimeConfigError(
      'config.json key "entraProtocol" must be "aad" or "oidc"',
    );
  }
  const apiBase = readString(body, "apiBase");
  if (!apiBase.startsWith("/") || apiBase.startsWith("//")) {
    throw new RuntimeConfigError(
      'config.json key "apiBase" must be a same-origin path',
    );
  }
  const version = "version" in body ? readString(body, "version") : "dev";
  if (!VERSION_PATTERN.test(version)) {
    throw new RuntimeConfigError(
      'config.json key "version" is not a platform version',
    );
  }
  return {
    entraEnabled: readBoolean(body, "entraEnabled"),
    entraClientId: readString(body, "entraClientId"),
    entraAuthority: readString(body, "entraAuthority"),
    entraScopes: readString(body, "entraScopes")
      .split(" ")
      .filter((scope) => scope !== ""),
    entraProtocol: protocol,
    localLoginEnabled: readBoolean(body, "localLoginEnabled"),
    apiBase,
    version,
  };
}

export async function loadRuntimeConfig(
  fetchImpl: FetchLike = (input, init) => fetch(input, init),
): Promise<RuntimeConfig> {
  let response: Response;
  try {
    response = await fetchImpl("/config.json", { cache: "no-store" });
  } catch (error) {
    throw new RuntimeConfigError("config.json could not be fetched", {
      cause: error,
    });
  }
  if (!response.ok) {
    throw new RuntimeConfigError(
      `config.json answered HTTP ${String(response.status)}`,
    );
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    throw new RuntimeConfigError("config.json is not valid JSON", {
      cause: error,
    });
  }
  return parseRuntimeConfig(body);
}
