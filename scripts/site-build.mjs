// Validate the development-preview public page without stamping test counts.
//
// A local proof ledger can establish narrow source-bound contracts. It cannot turn
// a web page into evidence of live money, physical supply, market liquidity,
// distribution, or production readiness. The old build step copied a ledger count
// into web/index.html; that made a changing implementation-test total look like a
// product score and also mutated source after the proof run. This replacement is
// deliberately read-only: current claims are governed by proof/5x5-gates.json and
// scripts/validate_claims.py, while this check prevents the site markup from
// reintroducing a hand-typed proof counter.

import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const SITE_PATH = path.join(ROOT, 'web/index.html');

function die(message) {
  console.error(`site-build: ${message}`);
  process.exit(1);
}

if (!fs.existsSync(SITE_PATH)) {
  die(`site source not found at ${path.relative(ROOT, SITE_PATH)}`);
}

const html = fs.readFileSync(SITE_PATH, 'utf8');
for (const fragment of [
  'Development preview',
  'No live-money, market-liquidity, signed-distribution, or physical-fleet claim',
  'proof/5x5-gates.json',
]) {
  if (!html.includes(fragment)) {
    die(`missing required development boundary ${JSON.stringify(fragment)}`);
  }
}

const staticCounter = /\b\d+\s*\/\s*\d+\s+(?:pass(?:ed)?|checks?)\b|\b\d+\s+(?:pass(?:ed)?|checks?)\b/i;
const match = html.match(staticCounter);
if (match) {
  die(`static proof counter ${JSON.stringify(match[0])} is forbidden; render a source-bound claim artifact instead`);
}

console.log(
  `site-build: validated ${path.relative(ROOT, SITE_PATH)} as a development preview; ` +
    'no proof counter was stamped and no source file was modified',
);
