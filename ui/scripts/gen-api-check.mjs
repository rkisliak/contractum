// `pnpm gen:api:check` (UI-06, D-UI-08-30, T-8-20): regenerate the TypeScript types from the
// committed snapshot to a temporary file and fail when they differ from the committed
// src/api/schema.gen.ts. It compares bytes, so it needs no git and also works inside the
// Docker `ci` stage.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const uiRoot = resolve(import.meta.dirname, "..");
const snapshot = join(uiRoot, "openapi", "openapi.json");
const committed = join(uiRoot, "src", "api", "schema.gen.ts");
const generator = join(uiRoot, "node_modules", ".bin", "openapi-typescript");
const hint =
  "src/api/schema.gen.ts is stale: run `uv run python scripts/export_openapi.py` then `pnpm --dir ui gen:api`";

const scratch = mkdtempSync(join(tmpdir(), "dc-gen-api-"));
try {
  const regenerated = join(scratch, "schema.gen.ts");
  const run = spawnSync(generator, [snapshot, "-o", regenerated], {
    cwd: uiRoot,
    stdio: ["ignore", "ignore", "inherit"],
  });
  if (run.error) {
    throw run.error;
  }
  if (run.status !== 0) {
    console.error("openapi-typescript failed");
    process.exit(run.status ?? 1);
  }
  let current;
  try {
    current = readFileSync(committed);
  } catch {
    console.error(hint);
    process.exit(1);
  }
  if (!current.equals(readFileSync(regenerated))) {
    console.error(hint);
    process.exit(1);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
