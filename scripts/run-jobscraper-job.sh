#!/usr/bin/env bash
# Push the REAL jobscraper corpus (473 scraped fintech postings) through the pipeline
# as a batch_classification job, against the stack prove-local left up (KEEP=1).
# Measures wall-clock + throughput + estimated-vs-actual. Honest: no faked numbers.
set -uo pipefail
ROOT=/Users/scammermike/Downloads/computexchange
ART="$ROOT/.artifacts/prove-local"
URL="http://localhost:18080"
KEY="dev-api-key-0001"
INPUT="$ROOT/.artifacts/jobscraper.jsonl"
AGENT_LOG="$ROOT/.artifacts/agent-jobscraper.log"
cd "$ROOT"

[ -f "$INPUT" ] || { echo "missing $INPUT"; exit 1; }
curl -fsS "$URL/v1/models" -H "Authorization: Bearer $KEY" >/dev/null 2>&1 || { echo "control not up at $URL (run prove-local KEEP=1 first)"; exit 1; }

echo "== submit batch_classification over $(wc -l < "$INPUT") real postings =="
SUBMIT=$(python3 - <<'PY'
import json,urllib.request,os
inp=open(os.path.expanduser('~/Downloads/computexchange/.artifacts/jobscraper.jsonl')).read()
body={"job_type":{"type":"batch_classification","labels":["engineering","finance","hybrid","ops","product","strategy","other"]},
 "model":{"kind":"gguf","ref":"llama-3.2-1b-instruct-q4"},"params":{},
 "constraints":{"min_memory_gb":0,"max_duration_secs":0},
 "verification":{"redundancy_frac":0,"honeypot_frac":0,"payout_hold_secs":0},
 "tier":"batch","input":inp}
req=urllib.request.Request("http://localhost:18080/v1/jobs",data=json.dumps(body).encode(),
 method="POST",headers={"Authorization":"Bearer dev-api-key-0001","Content-Type":"application/json"})
try:
    print(urllib.request.urlopen(req,timeout=60).read().decode())
except urllib.error.HTTPError as e:
    print("HTTPERR",e.code,e.read().decode()[:300])
PY
)
echo "  $SUBMIT"
JOB=$(echo "$SUBMIT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
[ -n "$JOB" ] || { echo "no job_id (submit failed)"; exit 1; }
echo "$SUBMIT" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  predicted: %d tasks, est \$%.4f, eta %ss'%(d.get('task_count',0),d.get('estimated_usd',0),d.get('eta_secs',0)))"

# Ensure an agent is running (prove-local may have left one up).
if ! pgrep -f "cx-agent run" >/dev/null 2>&1; then
  echo "== starting agent =="
  ( exec "$ROOT/agent/target/release/cx-agent" run --config "$ART/agent.toml" ) >"$AGENT_LOG" 2>&1 &
  sleep 4
else
  echo "== agent already running (prove-local KEEP) =="
  AGENT_LOG="$ART/agent.log"
fi

echo "== poll =="
START=$(python3 -c "import time;print(int(time.time()))")
FINAL=""
for i in $(seq 1 300); do
  S=$(curl -fsS "$URL/v1/jobs/$JOB" -H "Authorization: Bearer $KEY" 2>/dev/null)
  LINE=$(echo "$S" | python3 -c "import sys,json
d=json.load(sys.stdin)
print('%s %s/%s actual=\$%.4f'%(d.get('status'),d.get('tasks_done'),d.get('task_count'),d.get('actual_usd') or 0))" 2>/dev/null)
  [ "$((i % 5))" = "1" ] && echo "  [${i}] $LINE"
  case "$LINE" in complete*) FINAL=complete; break;; failed*|cancelled*) FINAL="$LINE"; break;; esac
  sleep 2
done
END=$(python3 -c "import time;print(int(time.time()))")
echo "== done in $((END-START))s :: $FINAL =="
echo "== throughput (agent log) =="; grep -iE "tps|tok/s|classif|generate_batch|batch.*infer|eps" "$AGENT_LOG" 2>/dev/null | tail -8
echo "== sample classifications =="
curl -fsS "$URL/v1/jobs/$JOB/results" -H "Authorization: Bearer $KEY" 2>/dev/null | head -c 500; echo
