import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Offline supply-chain guard (T-8-SC): a loose range or a drifting toolchain
// version can never slip into ui/package.json unnoticed.
interface PackageJson {
  packageManager: string;
  engines: { node: string };
  dependencies: Record<string, string>;
  devDependencies: Record<string, string>;
}

const uiRoot = resolve(import.meta.dirname, "..", "..");
const pkg = JSON.parse(
  readFileSync(resolve(uiRoot, "package.json"), "utf8"),
) as PackageJson;
const nvmrc = readFileSync(resolve(uiRoot, ".nvmrc"), "utf8").trim();

const EXACT = /^\d+\.\d+\.\d+$/;

describe("pinned toolchain", () => {
  it.each([
    ["dependencies", pkg.dependencies],
    ["devDependencies", pkg.devDependencies],
  ])("every %s version is an exact x.y.z", (_group, deps) => {
    const entries = Object.entries(deps);
    expect(entries.length).toBeGreaterThan(0);
    const loose = entries.filter(([, version]) => !EXACT.test(version));
    expect(loose).toEqual([]);
  });

  it("pins pnpm through packageManager", () => {
    expect(pkg.packageManager).toBe("pnpm@12.23.0");
  });

  it("keeps engines.node equal to .nvmrc", () => {
    expect(nvmrc).toMatch(EXACT);
    expect(pkg.engines.node).toBe(nvmrc);
  });

  it("pins the editor packages at the versions recorded in ui/CLAUDE.md", () => {
    expect(pkg.dependencies["monaco-editor"]).toBe("0.57.0");
    expect(pkg.dependencies["@monaco-editor/react"]).toBe("4.7.0");
  });
});
