// Supply-chain guard (T-8-SC): every dependency in ui/package.json must be an
// exact version, and every pinned version must have been published at least
// MIN_AGE_DAYS days ago. Dependency-free on purpose: it runs before the first
// install. Usage: node ui/scripts/check-pin-age.mjs
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const MIN_AGE_DAYS = 14;
const DAY_MS = 24 * 60 * 60 * 1000;
const EXACT = /^\d+\.\d+\.\d+$/;

const pkgPath = resolve(import.meta.dirname, "..", "package.json");
const pkg = JSON.parse(readFileSync(pkgPath, "utf8"));
const deps = { ...pkg.dependencies, ...pkg.devDependencies };

const failures = [];
const rows = [];
const now = Date.now();

async function check([name, version]) {
  if (!EXACT.test(version)) {
    failures.push(`${name}@${version}: not an exact version`);
    return;
  }
  const url = `https://registry.npmjs.org/${name.replace("/", "%2F")}`;
  const res = await fetch(url);
  if (!res.ok) {
    failures.push(`${name}: registry answered ${String(res.status)}`);
    return;
  }
  const doc = await res.json();
  const published = doc.time?.[version];
  if (published === undefined) {
    failures.push(
      `${name}@${version}: version has no publish time in the registry`,
    );
    return;
  }
  const ageDays = (now - new Date(published).getTime()) / DAY_MS;
  rows.push(
    `${name}@${version} published ${published.slice(0, 10)} (${ageDays.toFixed(1)} d)`,
  );
  if (ageDays < MIN_AGE_DAYS) {
    failures.push(
      `${name}@${version}: only ${ageDays.toFixed(1)} days old (< ${String(MIN_AGE_DAYS)})`,
    );
  }
}

const entries = Object.entries(deps);
const pool = 6;
let next = 0;
await Promise.all(
  Array.from({ length: pool }, async () => {
    while (next < entries.length) {
      const entry = entries[next++];
      if (entry !== undefined) await check(entry);
    }
  }),
);

rows.sort().forEach((r) => {
  console.log(r);
});
if (failures.length > 0) {
  console.error("\nFAIL");
  failures.forEach((f) => {
    console.error(`  ${f}`);
  });
  process.exit(1);
}
console.log(
  `\nOK: ${String(entries.length)} pins, all exact and at least ${String(MIN_AGE_DAYS)} days old`,
);
