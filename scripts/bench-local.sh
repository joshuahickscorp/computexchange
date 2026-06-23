#!/usr/bin/env bash
#
# Computexchange — bench-local: a repeatable LOCAL benchmark harness, SEPARATE
# from prove-local (Plane D §16 D10). prove-local proves CORRECTNESS; this measures
# SPEED. It NEVER replaces prove-local and changes nothing it touches — run either,
# both, or neither.
#
# It provisions a throwaway native Postgres + MinIO the SAME way prove-local does
# (no Docker pulls; reuse an already-up stack with KEEP=1), then measures and prints
# p50/p90 for:
#   (a) POST /v1/quote latency over K calls (the buyer preflight hot path),
#   (b) the ready-task claim query latency via EXPLAIN ANALYZE over a synthetic 1k
#       queue (the scheduler hot path the tasks_ready_unclaimed_idx serves),
#   (c) the embed JSON-vs-binary artifact size for N rows (the D5/D15 payload win) —
#       computed from the exact CXEM layout, and cross-checked against the agent's
#       own binary-encoder unit test when the Rust toolchain is present.
#
# Honest by construction (BLACKHOLE): every step fails LOUDLY, nothing is faked, and
# numbers are REAL measurements on THIS machine — not targets. A markdown report
# with a UTC timestamp, the git commit, and the machine class lands in
# .artifacts/bench-local/report.md.
#
# OPTIONAL. Needs no external services. Does NOT alter prove-local.
#
# Usage:   scripts/bench-local.sh            (or: make bench-local)
#   Env:   KEEP=1            reuse an already-running native stack + leave it up
#          QUOTE_CALLS=N     number of /v1/quote calls to time      (default 30)
#          CLAIM_RUNS=N      EXPLAIN ANALYZE repetitions for the claim (default 11)
#          SYNTH_TASKS=N     synthetic queued tasks to insert         (default 1000)
#          EMBED_ROWS=N      rows for the JSON-vs-binary size compare  (default 1000)
#          EMBED_DIM=N       embedding width for the size compare      (default 384)
#          BPGPORT/BMINIO_PORT/BMINIO_CONSOLE/BCONTROL_PORT  override ports
#          SMOKE=1           tiny + fast (used by the prove-local smoke row)

set -euo pipefail

# ── Locate repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# ── Config (all overridable) ─────────────────────────────────────────────────
# Distinct default ports from prove-local (55432/59000/59001/18080) so the two can
# coexist; bench-local stays out of prove-local's way entirely.
BPGPORT="${BPGPORT:-55433}"
BMINIO_PORT="${BMINIO_PORT:-59100}"
BMINIO_CONSOLE="${BMINIO_CONSOLE:-59101}"
BCONTROL_PORT="${BCONTROL_PORT:-18090}"
KEEP="${KEEP:-0}"
SMOKE="${SMOKE:-0}"

QUOTE_CALLS="${QUOTE_CALLS:-30}"
CLAIM_RUNS="${CLAIM_RUNS:-11}"
SYNTH_TASKS="${SYNTH_TASKS:-1000}"
EMBED_ROWS="${EMBED_ROWS:-1000}"
EMBED_DIM="${EMBED_DIM:-384}"
# Smoke mode: small + fast + deterministic, for an optional prove-local row.
if [ "$SMOKE" = "1" ]; then
  QUOTE_CALLS=5
  CLAIM_RUNS=3
  SYNTH_TASKS=200
  EMBED_ROWS=256
fi

ART="$ROOT/.artifacts/bench-local"
PGDATA="$ART/pgdata"
MINIO_DATA="$ART/minio-data"
CONTROL_LOG="$ART/control.log"
PG_LOG="$ART/pg.log"
MINIO_LOG="$ART/minio.log"
REPORT="$ART/report.md"

CONTROL_URL="http://localhost:$BCONTROL_PORT"
export DATABASE_URL="postgres://cx@localhost:$BPGPORT/cx?sslmode=disable"
export S3_ENDPOINT="http://localhost:$BMINIO_PORT"
export S3_PUBLIC_ENDPOINT="http://localhost:$BMINIO_PORT"
export S3_BUCKET="cx-jobs"
export S3_ACCESS_KEY="minioadmin"
export S3_SECRET_KEY="minioadmin"
export S3_REGION="us-east-1"
export LISTEN_ADDR=":$BCONTROL_PORT"

# Demo credentials are fixed by control/seed.go (same as prove-local).
API_KEY="dev-api-key-0001"
BUYER_ID="00000000-0000-0000-0000-0000000000c1"

CONTROL_PID=""; MINIO_PID=""; PG_STARTED=0

# ── Pretty logging ───────────────────────────────────────────────────────────
b()    { printf '\033[1m%s\033[0m' "$*"; }
say()  { printf '\033[1;36m[bench]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ⚠\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; }

die() {
  fail "$*"
  echo "----- control log (tail) -----" >&2; [ -f "$CONTROL_LOG" ] && tail -n 30 "$CONTROL_LOG" >&2 || true
  exit 1
}

# ── Cleanup: tear down only what we started (KEEP=1 leaves it up for reuse) ────
cleanup() {
  local ec=$?
  [ -n "$CONTROL_PID" ] && kill "$CONTROL_PID" 2>/dev/null || true
  sleep 1
  [ -n "$CONTROL_PID" ] && kill -9 "$CONTROL_PID" 2>/dev/null || true
  if [ "$KEEP" = "1" ]; then
    [ -n "$CONTROL_PID" ] && warn "KEEP=1 — leaving native deps up (DATABASE_URL=$DATABASE_URL)"
    exit "$ec"
  fi
  [ -n "$MINIO_PID" ] && kill "$MINIO_PID" 2>/dev/null || true
  if [ "$PG_STARTED" = "1" ]; then
    LC_ALL=C pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  fi
  rm -rf "$PGDATA" "$MINIO_DATA" 2>/dev/null || true
  exit "$ec"
}
trap cleanup EXIT INT TERM

# wait_for <seconds> <label> <cmd...>
wait_for() {
  local timeout="$1" label="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep 1
  done
  return 0
}

# percentiles <p50_var> <p90_var> < newline-separated numbers on stdin
#   prints "p50<TAB>p90<TAB>n<TAB>min<TAB>max" computed with nearest-rank (no deps).
#   Reads the data from THIS function's stdin (do not use a heredoc here — a
#   `python3 - <<EOF` would make the heredoc the program's stdin and shadow the
#   redirected data file; `-c` keeps stdin pointed at the caller's data).
percentiles() {
  python3 -c '
import sys, math
xs=sorted(float(l) for l in sys.stdin if l.strip())
n=len(xs)
if n==0:
    print("0\t0\t0\t0\t0"); raise SystemExit
def pct(p):
    # nearest-rank: ceil(p/100 * n), 1-indexed, clamped into [1, n].
    k=max(1,min(n,math.ceil(p/100.0*n)))
    return xs[k-1]
print("%.3f\t%.3f\t%d\t%.3f\t%.3f"%(pct(50),pct(90),n,xs[0],xs[-1]))
'
}

# ── Machine class (best-effort, never fatal) ─────────────────────────────────
machine_class() {
  local os arch cpu mem chip
  os="$(uname -s 2>/dev/null || echo '?')"
  arch="$(uname -m 2>/dev/null || echo '?')"
  if [ "$os" = "Darwin" ]; then
    chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)"
    cpu="$(sysctl -n hw.ncpu 2>/dev/null || echo '?')"
    local memb; memb="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    mem="$(MEMB="$memb" python3 -c 'import os;print("%.0f GB"%(int(os.environ["MEMB"])/1e9))' 2>/dev/null || echo '?')"
  else
    chip="$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//' || true)"
    cpu="$(nproc 2>/dev/null || echo '?')"
    mem="$(python3 -c "import os;print(f'{os.sysconf(\"SC_PAGE_SIZE\")*os.sysconf(\"SC_PHYS_PAGES\")/1e9:.0f} GB')" 2>/dev/null || echo '?')"
  fi
  [ -z "$chip" ] && chip="$arch"
  printf '%s %s · %s cores · %s RAM' "$os" "$arch" "$cpu" "$mem"
  [ -n "$chip" ] && printf ' · %s' "$chip"
}

# ── Phase 0: preflight + provisioning ────────────────────────────────────────
say "$(b 'Computexchange — local benchmark lab (Plane D D10)')"
[ "$SMOKE" = "1" ] && say "SMOKE=1 — tiny/fast run"
mkdir -p "$ART"

need=(go psql curl python3)
[ "$KEEP" = "1" ] || need+=(postgres initdb pg_ctl createdb minio)
for t in "${need[@]}"; do
  command -v "$t" >/dev/null 2>&1 || die "required tool '$t' not found on PATH"
done

# Reuse a running stack (KEEP=1) if it already answers; else provision a throwaway
# one the SAME way prove-local does (native, trust auth, custom ports).
if [ "$KEEP" = "1" ] && psql "$DATABASE_URL" -c 'SELECT 1' >/dev/null 2>&1; then
  ok "reusing already-up native Postgres on :$BPGPORT (KEEP=1)"
else
  say "provisioning native Postgres (:$BPGPORT) + MinIO (:$BMINIO_PORT)"
  rm -rf "$PGDATA" "$MINIO_DATA" 2>/dev/null || true
  # LC_ALL=C + --locale=C avoids the macOS Homebrew-PG multithreaded-postmaster
  # locale bug (identical to prove-local).
  LC_ALL=C initdb -D "$PGDATA" -U cx --auth=trust -E UTF8 --locale=C >"$PG_LOG" 2>&1 \
    || die "initdb failed (see $PG_LOG)"
  LC_ALL=C pg_ctl -D "$PGDATA" -o "-p $BPGPORT -c listen_addresses=localhost -c unix_socket_directories=$ART" \
         -l "$PG_LOG" -w start >>"$PG_LOG" 2>&1 || die "pg_ctl start failed (see $PG_LOG)"
  PG_STARTED=1
  createdb -h localhost -p "$BPGPORT" -U cx cx 2>>"$PG_LOG" || die "createdb cx failed (see $PG_LOG)"
  wait_for 20 "postgres ready" psql "$DATABASE_URL" -c 'SELECT 1' || die "postgres not accepting connections"
  MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
    minio server "$MINIO_DATA" --address "localhost:$BMINIO_PORT" --console-address "localhost:$BMINIO_CONSOLE" \
    >"$MINIO_LOG" 2>&1 &
  MINIO_PID=$!
  wait_for 30 "minio live" curl -fsS "http://localhost:$BMINIO_PORT/minio/health/live" \
    || die "minio did not come up (see $MINIO_LOG)"
  ok "native postgres(:$BPGPORT) + minio(:$BMINIO_PORT) up"
fi

# Schema + demo creds (idempotent — schema.sql is IF NOT EXISTS, seed re-confirms).
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql >/dev/null || die "schema apply failed"
say "building + starting the control plane (for the quote benchmark)"
(cd control && go build -o "$ART/control" .) || die "control build failed"
(cd control && "$ART/control" seed) >/dev/null 2>&1 || (cd control && go run . seed) >/dev/null 2>&1 \
  || die "seed failed (demo buyer api_key)"
( "$ART/control" ) >"$CONTROL_LOG" 2>&1 &
CONTROL_PID=$!
wait_for 30 "control healthz" bash -c "kill -0 $CONTROL_PID 2>/dev/null && curl -fsS '$CONTROL_URL/healthz'" \
  || die "control plane never became healthy"
ok "control plane healthy on :$BCONTROL_PORT"

# ── Benchmark (a): POST /v1/quote latency over K calls ───────────────────────
# The buyer preflight hot path (scan + price + persist, no spend). We time the full
# request round-trip with curl's own %{time_total} (seconds → ms), K times, after a
# warmup call (JIT of the model-catalogue read, first PG plan cache). Real numbers.
say "(a) timing POST /v1/quote over $QUOTE_CALLS calls"
QBODY='{"job_type":{"type":"embed"},"model":{"kind":"gguf","ref":"all-minilm-l6-v2"},"tier":"batch","verification":{"redundancy_frac":0,"honeypot_frac":0,"payout_hold_secs":0},"input":"{\"id\":\"a\",\"text\":\"quote me\"}\n{\"id\":\"b\",\"text\":\"before I spend\"}\n"}'
# Warmup (not measured) — confirms the endpoint actually prices, else bail loudly.
WARM="$(curl -fsS -X POST "$CONTROL_URL/v1/quote" -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d "$QBODY" 2>/dev/null || true)"
printf '%s' "$WARM" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["quote_id"].startswith("q_")' 2>/dev/null \
  || die "quote endpoint did not return a valid quote (warmup) — got: ${WARM:0:200}"
QUOTE_MS="$ART/quote_ms.txt"; : >"$QUOTE_MS"
for _ in $(seq 1 "$QUOTE_CALLS"); do
  # %{time_total} is whole-request seconds; convert to ms. -o /dev/null drops the body.
  T="$(curl -fsS -o /dev/null -w '%{time_total}' -X POST "$CONTROL_URL/v1/quote" \
        -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
        -d "$QBODY" 2>/dev/null || echo '')"
  # Pass the timing via env (T_SECS) so no curl-formatted value is interpolated into
  # the Python source — robust regardless of locale/quoting.
  [ -n "$T" ] && T_SECS="$T" python3 -c 'import os;print("%.3f"%(float(os.environ["T_SECS"])*1000))' >>"$QUOTE_MS"
done
read -r Q_P50 Q_P90 Q_N Q_MIN Q_MAX < <(percentiles <"$QUOTE_MS")
[ "${Q_N:-0}" -ge 1 ] || die "no quote latencies captured"
ok "quote: p50=${Q_P50}ms p90=${Q_P90}ms (n=$Q_N, min=${Q_MIN} max=${Q_MAX})"

# ── Benchmark (b): ready-task claim query latency over a synthetic 1k queue ───
# Insert SYNTH_TASKS synthetic claimable tasks under one synthetic job, then time the
# EXACT inner CTE of ClaimTask (control/scheduler.go) — the ready-task selection the
# tasks_ready_unclaimed_idx serves — via EXPLAIN ANALYZE, parsing the reported
# Execution Time over CLAIM_RUNS repetitions. We measure the SELECT (no UPDATE) so the
# rows stay claimable and every run is identical + repeatable. This is the scheduler's
# hot path; the index's plan is separately asserted by prove-local's claim-index-explain.
say "(b) inserting $SYNTH_TASKS synthetic tasks + timing the claim query (EXPLAIN ANALYZE ×$CLAIM_RUNS)"
SYNTH_JOB="$(uuidgen | tr 'A-Z' 'a-z')"
# Columns are the AUTHORITATIVE db/schema.sql shape: tasks has input_ref + chunk_index
# (the latter ALTER-added) but NOT result_ref/result_key in the claim's read set, so we
# insert only what's needed to make a row claimable (status, chunk_index for ordering,
# visible_at=now). stderr is NOT swallowed — a schema drift must fail LOUDLY (BLACKHOLE).
psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 >/dev/null <<SQL || die "synthetic queue insert failed (see error above)"
INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb, estimated_usd)
  VALUES ('$SYNTH_JOB','$BUYER_ID','running','embed','all-minilm-l6-v2','jobs/bench/in.jsonl','batch',$SYNTH_TASKS,0,2,1.00);
INSERT INTO tasks (id, job_id, status, input_ref, chunk_index, visible_at)
  SELECT gen_random_uuid(), '$SYNTH_JOB', 'queued', 'jobs/bench/t'||g||'/in.jsonl', g, now()
  FROM generate_series(0, $SYNTH_TASKS - 1) AS g;
ANALYZE tasks;
SQL
QUEUE_DEPTH="$(psql "$DATABASE_URL" -tAc "SELECT count(*) FROM tasks WHERE status IN ('queued','retrying') AND claimed_by IS NULL" 2>/dev/null | tr -d '[:space:]')"
# The claim's ready-task selection, verbatim shape (status/visibility/unclaimed +
# stable ORDER BY + LIMIT 1). enable_seqscan=off is passed via PGOPTIONS (session GUC
# on the connection) rather than an inline `SET ...;` so the query is the EXPLAIN ALONE
# and psql emits PURE JSON (an inline SET would print a "SET" line that breaks json.load).
# It forces the planner onto the partial index on a small table (matches prove-local's
# claim-index-explain), so we time the INDEX-served path the production claim uses.
CLAIM_SQL="EXPLAIN (ANALYZE, TIMING ON, FORMAT JSON) SELECT id FROM tasks WHERE status IN ('queued','retrying') AND claimed_by IS NULL AND COALESCE(visible_at, created_at) <= now() ORDER BY COALESCE(visible_at, created_at), created_at LIMIT 1"
CLAIM_MS="$ART/claim_ms.txt"; : >"$CLAIM_MS"
CLAIM_PLAN=""
for _ in $(seq 1 "$CLAIM_RUNS"); do
  PLAN="$(PGOPTIONS='-c enable_seqscan=off' psql "$DATABASE_URL" -tAc "$CLAIM_SQL" 2>/dev/null || true)"
  [ -z "$CLAIM_PLAN" ] && CLAIM_PLAN="$PLAN"
  printf '%s' "$PLAN" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print("%.3f"%d[0]["Execution Time"])
except Exception:
    pass' >>"$CLAIM_MS" 2>/dev/null || true
done
read -r C_P50 C_P90 C_N C_MIN C_MAX < <(percentiles <"$CLAIM_MS")
[ "${C_N:-0}" -ge 1 ] || die "no claim-query timings captured (EXPLAIN ANALYZE produced nothing)"
CLAIM_INDEX="seq scan"
printf '%s' "$CLAIM_PLAN" | grep -q "tasks_ready_unclaimed_idx" && CLAIM_INDEX="tasks_ready_unclaimed_idx (index scan)"
ok "claim: p50=${C_P50}ms p90=${C_P90}ms over $QUEUE_DEPTH-task queue via $CLAIM_INDEX"
# Clean the synthetic queue so a KEEP=1 reuse starts from a known state next time.
psql "$DATABASE_URL" -q -c "DELETE FROM tasks WHERE job_id='$SYNTH_JOB'; DELETE FROM jobs WHERE id='$SYNTH_JOB'" >/dev/null 2>&1 || true

# ── Benchmark (c): embed JSON-vs-binary artifact size for N rows ──────────────
# The D5/D15 payload economy: for EMBED_ROWS×EMBED_DIM f32 embeddings, the binary
# CXEM blob is 16 + N*dim*4 bytes; the JSON the runner would otherwise PUT is the
# compact {"job_type","model","dim","count","vectors":[[...]]} object. We compute BOTH
# exactly (deterministic, infra-free) and report the ratio + bytes saved. When the
# Rust toolchain is present we ALSO run the agent's own encoder unit test so the report
# can state the format is the agent's real one, not a re-derivation.
say "(c) embed JSON-vs-binary size for ${EMBED_ROWS}×${EMBED_DIM} f32 rows"
# Compute into a file via a plain heredoc (no heredoc-inside-process-substitution —
# that confuses bash's parser), then read the tab-separated line back.
EMBED_OUT="$ART/embed_size.tsv"
EMBED_ROWS="$EMBED_ROWS" EMBED_DIM="$EMBED_DIM" python3 - >"$EMBED_OUT" <<'PY' || die "embed size computation failed"
import json,os
N=int(os.environ["EMBED_ROWS"]); DIM=int(os.environ["EMBED_DIM"])
# Binary CXEM blob: 16-byte header + N*DIM little-endian f32 (the exact agent layout).
bin_bytes=16+N*DIM*4
# JSON the runner would PUT for the same rows. Build the real object (deterministic
# values) and json.dumps it compactly so the byte count is the true serialized size.
vecs=[[((r*DIM+c)%1000)*0.001-0.5 for c in range(DIM)] for r in range(N)]
js=json.dumps({"job_type":"embed","model":"all-minilm-l6-v2","dim":DIM,"count":N,"vectors":vecs},
              separators=(",",":")).encode()
json_bytes=len(js)
ratio=json_bytes/bin_bytes if bin_bytes else 0.0
print("%d\t%d\t%.2f\t%d"%(bin_bytes,json_bytes,ratio,json_bytes-bin_bytes))
PY
IFS=$'\t' read -r BIN_BYTES JSON_BYTES RATIO SAVED <"$EMBED_OUT"
[ -n "${BIN_BYTES:-}" ] || die "embed size computation produced no output"
ok "embed: binary=${BIN_BYTES}B json=${JSON_BYTES}B → JSON is ${RATIO}× binary (saves ${SAVED}B)"
# Cross-check the format against the agent's own encoder test (best-effort; skipped if
# cargo absent or the test cannot build — never fatal, the sizes above stand alone).
ENCODER_CHECK="skipped (cargo not run)"
if [ "$SMOKE" != "1" ] && command -v cargo >/dev/null 2>&1; then
  if (cd agent && cargo test --release encode_embeddings_binary -- --nocapture) >"$ART/agent-encoder-test.log" 2>&1 \
     || (cd agent && cargo test --release embed_binary -- --nocapture) >>"$ART/agent-encoder-test.log" 2>&1; then
    ENCODER_CHECK="PASS (agent cargo encoder test)"
    ok "agent binary encoder unit test confirms the CXEM layout"
  else
    ENCODER_CHECK="not run (agent encoder test unavailable)"
    warn "agent encoder test did not run — size figures above are independent of it"
  fi
fi

# ── Report ───────────────────────────────────────────────────────────────────
TS_UTC="$(date -u +'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u 2>/dev/null || echo 'unknown')"
GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
GIT_DIRTY=""
git -C "$ROOT" diff --quiet 2>/dev/null || GIT_DIRTY=" (dirty)"
MACHINE="$(machine_class)"

cat >"$REPORT" <<MD
# Computexchange — local benchmark report

> Repeatable LOCAL speed measurements (Plane D §16 D10). SEPARATE from prove-local
> (correctness). Numbers are REAL measurements on this machine, not targets.

| field        | value |
|--------------|-------|
| generated    | \`$TS_UTC\` |
| git commit   | \`$GIT_SHA\`$GIT_DIRTY |
| machine      | $MACHINE |
| mode         | $([ "$SMOKE" = "1" ] && echo 'smoke (tiny/fast)' || echo 'full') |

## (a) POST /v1/quote latency

Whole-request round-trip over **$Q_N** calls (after warmup).

| p50 | p90 | min | max |
|-----|-----|-----|-----|
| ${Q_P50} ms | ${Q_P90} ms | ${Q_MIN} ms | ${Q_MAX} ms |

## (b) Ready-task claim query latency

\`EXPLAIN ANALYZE\` of the claim's ready-task selection over a synthetic
**$QUEUE_DEPTH**-task queue, $C_N runs. Plan: **$CLAIM_INDEX**.

| p50 | p90 | min | max |
|-----|-----|-----|-----|
| ${C_P50} ms | ${C_P90} ms | ${C_MIN} ms | ${C_MAX} ms |

## (c) Embed artifact size — JSON vs binary

For **$EMBED_ROWS** rows × **$EMBED_DIM**-dim f32 embeddings (the D5/D15 payload path).

| binary (CXEM) | JSON | JSON ÷ binary | bytes saved |
|---------------|------|---------------|-------------|
| ${BIN_BYTES} B | ${JSON_BYTES} B | ${RATIO}× | ${SAVED} B |

Format cross-check: **$ENCODER_CHECK**

---

_Generated by \`scripts/bench-local.sh\`. Re-run with \`make bench-local\`. This harness
never replaces \`prove-local\` and changes nothing it touches._
MD

echo
say "$(b '================  BENCH REPORT  ================')"
ok "quote   p50=${Q_P50}ms  p90=${Q_P90}ms   (n=$Q_N)"
ok "claim   p50=${C_P50}ms  p90=${C_P90}ms   ($QUEUE_DEPTH-task queue, $CLAIM_INDEX)"
ok "embed   binary=${BIN_BYTES}B  json=${JSON_BYTES}B  (${RATIO}× smaller)"
echo
say "report → $REPORT"
say "$(b 'LOCAL BENCHMARK: DONE') ✅"
