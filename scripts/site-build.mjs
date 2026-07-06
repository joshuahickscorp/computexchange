// site-build.mjs · stamp the REAL prove-local pass count into the public page.
//
// Creed rung (Public Site & Conversion, 5 -> 6): the page's entire thesis is
// "receipts are generated, not typed" — and yet the "168 / 168 pass" figure in
// web/index.html was a hand-typed literal, re-typed by a human every time
// SITE-CLAIMS.md was refreshed. That is exactly the kind of gap the page's own
// thesis says should not exist.
//
// This build step closes it: it reads the REAL proof ledger that
// `scripts/prove-local.sh` writes at `.artifacts/prove-local/proof-ledger.txt`
// (a plain-text PASS/SKIP/FAIL-per-line ledger, plus two META lines this shift
// added — commit + started_at — recorded AT THE TIME prove-local actually ran),
// and rewrites the three places web/index.html asserts that count so they can
// never drift from what was actually proven.
//
// It deliberately does NOT re-run prove-local itself: prove-local boots a real
// Postgres + MinIO + the real agent against live Metal/Candle inference and
// takes minutes, so re-running it on every site build would make "edit a CSS
// rule, rebuild the site" a multi-minute op and would silently blur "the count
// the page shows" with "whatever prove-local felt like doing right now" instead
// of a specific, inspectable, checked-in-spirit proof run. Instead this script
// is the honest half of the same discipline: it refuses to guess. If the ledger
// is missing, it fails loudly and tells you to run `make prove-local` — it never
// falls back to a hand-typed or stale number.
//
// Usage:  node scripts/site-build.mjs
//   Env:  SITE_BUILD_ALLOW_STALE_COMMIT=1   stamp even if the ledger's recorded
//         commit differs from the working tree's current HEAD (still stamps the
//         ledger's OWN commit + timestamp, never today's HEAD by default —
//         the number must trace to the run that produced it, not to "now").

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const LEDGER_PATH = path.join(ROOT, '.artifacts/prove-local/proof-ledger.txt');
const SITE_PATH = path.join(ROOT, 'web/index.html');

function die(msg) {
  console.error(`site-build: ${msg}`);
  process.exit(1);
}

if (!fs.existsSync(LEDGER_PATH)) {
  die(
    `no proof ledger at ${path.relative(ROOT, LEDGER_PATH)} — run \`make prove-local\` ` +
      `(or \`bash scripts/prove-local.sh\`) first. This build step refuses to stamp a ` +
      `guessed or hand-typed pass count.`,
  );
}

const ledgerRaw = fs.readFileSync(LEDGER_PATH, 'utf8');
const lines = ledgerRaw.split('\n').filter(Boolean);

let pass = 0, skip = 0, fail = 0, commit = null, startedAt = null;
for (const line of lines) {
  const [status, cap, ...rest] = line.split('\t');
  if (status === 'PASS') pass++;
  else if (status === 'SKIP') skip++;
  else if (status === 'FAIL') fail++;
  else if (status === 'META' && cap === 'commit') commit = rest.join('\t');
  else if (status === 'META' && cap === 'started_at') startedAt = rest.join('\t');
}

if (pass + skip + fail === 0) {
  die(`proof ledger at ${path.relative(ROOT, LEDGER_PATH)} has zero PASS/SKIP/FAIL lines — is it truncated or from an old prove-local build?`);
}
if (!commit) {
  die(
    `proof ledger has no META commit line — it was produced by a prove-local.sh ` +
      `older than this build step's expectations. Re-run \`make prove-local\` with the ` +
      `current scripts/prove-local.sh (which now records META lines) to regenerate it.`,
  );
}
if (!startedAt) {
  die(`proof ledger has no META started_at line — re-run \`make prove-local\` to regenerate it.`);
}

// Warn (do not fail the build by default) if the ledger's commit differs from
// the working tree's current HEAD — the site can still legitimately ship a
// pass count proven on the previous commit while a docs-only commit lands on
// top, but a silent drift should never go unnoticed.
let headSha = null;
try {
  headSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
} catch {
  // no git available in this environment — stamp the ledger's own commit only.
}
if (headSha && commit !== headSha && process.env.SITE_BUILD_ALLOW_STALE_COMMIT !== '1') {
  console.warn(
    `site-build: WARNING — proof ledger was recorded at commit ${commit.slice(0, 12)}, ` +
      `but working tree HEAD is ${headSha.slice(0, 12)}. Stamping the page with the ` +
      `ledger's own commit (the number must trace to the run that produced it), not HEAD. ` +
      `Re-run \`make prove-local\` to refresh the proof against the current commit, or set ` +
      `SITE_BUILD_ALLOW_STALE_COMMIT=1 to silence this warning.`,
  );
}

const shortCommit = commit.slice(0, 12);
// startedAt is `date -u +%Y-%m-%dT%H:%M:%SZ` from prove-local.sh — display it
// as-is (already a real, machine-produced UTC ISO-8601 timestamp).
const provenance = `proven ${startedAt} · ${shortCommit}`;

if (!fs.existsSync(SITE_PATH)) die(`site source not found at ${path.relative(ROOT, SITE_PATH)}`);
let html = fs.readFileSync(SITE_PATH, 'utf8');

// Each pattern matches BOTH the original hand-typed markup and an already-
// stamped page (any digits, and an optional trailing "· matrix run" or a
// previous "· proven ...·<hash>" provenance suffix) — so re-running this
// build against a fresh prove-local pass is idempotent (including the no-op
// case where the ledger hasn't changed since the last build), not a one-shot
// migration that then refuses to run again. Correctness is checked by whether
// each pattern MATCHED, not by whether the replacement changed any bytes —
// re-stamping the same count at the same commit is a legitimate no-op.
const PROVENANCE_TAIL = /(?: · matrix run| · proven [^<]*)?/.source;

let matched = 0;

// 1 · the "how it works" console's inline proof row: `<span class="state">168 / 168 pass</span>`
{
  const re = /(<span class="stmt">results come back verified[^<]*<\/span><span class="state">)\d+ \/ \d+ pass(<\/span>)/;
  if (re.test(html)) matched++;
  html = html.replace(re, `$1${pass} / ${pass + skip + fail} pass$2`);
}

// 2 · the ledger dialog's summary strip (the .gen span carries provenance —
//     WHEN and at WHAT COMMIT this exact count was proven — not a restatement
//     of the pass/skip/fail figures already shown by the three spans before it):
//     <div class="ledger-stat"><span><b>168</b> pass</span><span>0 skip</span><span>0 fail</span><span class="gen">make prove-local · matrix run</span></div>
{
  const re = new RegExp(
    `(<div class="ledger-stat"><span><b>)\\d+(</b> pass</span><span>)\\d+( skip</span><span>)\\d+( fail</span><span class="gen">make prove-local)${PROVENANCE_TAIL}(</span></div>)`,
  );
  if (re.test(html)) matched++;
  html = html.replace(re, `$1${pass}$2${skip}$3${fail}$4 · ${provenance}$5`);
}

// 3 · the ledger dialog's "proof" claim row:
//     make prove-local · <b>168 pass · 0 skip · 0 fail</b> · a matrix-only run, tallied by grep over its own ledger
{
  const re = new RegExp(
    `(<span class="k">proof</span><span class="v">make prove-local · <b>)\\d+ pass · \\d+ skip · \\d+ fail(</b> · a matrix-only run, tallied by grep over its own ledger)${PROVENANCE_TAIL}(</span></div>)`,
  );
  if (re.test(html)) matched++;
  html = html.replace(re, `$1${pass} pass · ${skip} skip · ${fail} fail$2 · ${provenance}$3`);
}

if (matched < 3) {
  die(
    `only ${matched}/3 hand-typed pass-count markers matched in ${path.relative(ROOT, SITE_PATH)} — ` +
      `the page markup shape changed and this script's replacement patterns are stale. ` +
      `Update the three regexes in scripts/site-build.mjs to match the current markup.`,
  );
}

fs.writeFileSync(SITE_PATH, html);

console.log(
  `site-build: stamped ${path.relative(ROOT, SITE_PATH)} with ${pass} pass · ${skip} skip · ` +
    `${fail} fail, proven ${startedAt} at commit ${shortCommit} (from ${path.relative(ROOT, LEDGER_PATH)})`,
);
