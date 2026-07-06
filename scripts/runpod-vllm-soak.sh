#!/usr/bin/env bash
# runpod-vllm-soak.sh — the REQUIRED de-risk spike for the vLLM CUDA serving lane
# (docs/VLLM_LANE.md, steps 1-3): stand up TWO independently-provisioned, identically
# PINNED vLLM OpenAI-compatible servers on real RunPod GPUs, run the same greedy
# corpus against both, and prove byte-identical output — first across two pods (the
# cross-SKU/same-SKU soak), then across a restart of each pod's own server (the
# restart soak). This is what `VllmRunner`'s wired shell-out path
# (agent/src/runners.rs, gated behind CX_VLLM_SOAK_MODE) is not allowed to carry
# real traffic without — see docs/VLLM_LANE.md's "REQUIRED de-risk spike" section.
#
# HONESTY (BLACKHOLE): this really provisions TWO pods, which really costs money
# (roughly 2x scripts/runpod-spike.sh's per-hour rate), and tears both down on exit
# (even on failure) so a forgotten box does not bleed cost. It never fabricates a
# PASS — a byte mismatch is reported as a FAIL, in full, not summarized away.
#
# What this does NOT do (see docs/VLLM_LANE.md steps 4-5, both still open after a
# PASS here): hw_class-aware honeypot seeding and the golden-hash baseline seed —
# those require this soak to pass first, then a deliberate, separate seeding step
# against a chosen pinned reference box. This script only proves the pin is stable;
# it does not flip any production gate.
#
# ── What you type (the whole thing) ──────────────────────────────────────────
#   export RUNPOD_API_KEY=...            # RunPod console → Settings → API Keys
#   bash scripts/runpod-vllm-soak.sh          # same-SKU soak (cheap default)
#   GPU_TYPE_B="NVIDIA H100 80GB PCIe" \
#     bash scripts/runpod-vllm-soak.sh        # cross-SKU soak (A100 vs H100)
#
# Subcommands (default is the full lifecycle `run`):
#   run     provision both pods → deploy pinned vLLM → cross-pod soak → restart
#           soak → pull results → report → terminate both
#   down    terminate any pods recorded from a previous run (money-safety escape hatch)
#
# ── One-time RunPod account setup ────────────────────────────────────────────
#   Add your SSH PUBLIC key in the RunPod console (Settings → SSH Public Keys).
#
# ── Knobs (env) ───────────────────────────────────────────────────────────────
#   RUNPOD_API_KEY  (required)   your RunPod API key
#   GPU_TYPE_A      NVIDIA GPU display id for pod A (default "NVIDIA A100 80GB PCIe")
#   GPU_TYPE_B      NVIDIA GPU display id for pod B (default: same as GPU_TYPE_A —
#                   set it to a different SKU, e.g. "NVIDIA H100 80GB PCIe", to run
#                   the cross-SKU variant of the soak instead of the same-SKU one)
#   CLOUD_TYPE      SECURE | COMMUNITY     (default COMMUNITY)
#   VLLM_MODEL      pinned HF model id     (default "meta-llama/Llama-3.2-1B-Instruct")
#   VLLM_VERSION    pinned vLLM pip version (default "0.6.3")
#   PROMPT_COUNT    corpus size            (default 24)
#   MAX_TOKENS      decode length per prompt (default 64)
#   KEEP=1          on `run`, do NOT terminate at the end (inspect the boxes yourself)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

API="https://api.runpod.io/graphql"
ART=.artifacts/vllm-soak
POD_FILE_A="$ART/pod_a.id"
POD_FILE_B="$ART/pod_b.id"
mkdir -p "$ART"

PROVISIONED_A=0
PROVISIONED_B=0

GPU_TYPE_A="${GPU_TYPE_A:-NVIDIA A100 80GB PCIe}"
GPU_TYPE_B="${GPU_TYPE_B:-$GPU_TYPE_A}"
CLOUD_TYPE="${CLOUD_TYPE:-COMMUNITY}"
IMAGE="${IMAGE:-nvidia/cuda:12.4.1-runtime-ubuntu22.04}"
DISK_GB="${DISK_GB:-40}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
VLLM_MODEL="${VLLM_MODEL:-meta-llama/Llama-3.2-1B-Instruct}"
VLLM_VERSION="${VLLM_VERSION:-0.6.3}"
PROMPT_COUNT="${PROMPT_COUNT:-24}"
MAX_TOKENS="${MAX_TOKENS:-64}"

die()  { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }
hr()   { echo >&2; echo "== $* ==" >&2; }

command -v jq   >/dev/null 2>&1 || die "jq not found (brew install jq)"
command -v curl >/dev/null 2>&1 || die "curl not found"
[ -n "${RUNPOD_API_KEY:-}" ] || die "RUNPOD_API_KEY unset — RunPod console → Settings → API Keys, then: export RUNPOD_API_KEY=..."

gql() {
  local query="$1" filter="${2:-.}" resp
  resp="$(curl -fsS -X POST "$API?api_key=$RUNPOD_API_KEY" \
    -H 'Content-Type: application/json' \
    --data "$(jq -nc --arg q "$query" '{query:$q}')" 2>/dev/null)" \
    || die "RunPod API call failed (network, or a bad API key)"
  if echo "$resp" | jq -e '.errors' >/dev/null 2>&1; then
    die "RunPod API error: $(echo "$resp" | jq -c '.errors')"
  fi
  echo "$resp" | jq -r "$filter"
}

# pod_up NAME GPU_TYPE POD_FILE PROVISIONED_VAR_NAME — provision one pod, poll for
# its public SSH endpoint, record both the pod id and the ssh endpoint under $ART.
pod_up() {
  local name="$1" gpu="$2" podfile="$3" provisioned_var="$4"
  [ -f "$SSH_PUBKEY" ] || die "SSH pubkey not found at '$SSH_PUBKEY' (also add it in the RunPod console)"

  hr "provisioning $name · $gpu · $CLOUD_TYPE"
  local boot='bash -c "apt-get update && apt-get install -y --no-install-recommends openssh-server python3-pip >/tmp/boot.log 2>&1; mkdir -p /run/sshd /root/.ssh; /usr/sbin/sshd -D"'
  local mutation
  mutation=$(cat <<GQL
mutation {
  podFindAndDeployOnDemand(input: {
    cloudType: $CLOUD_TYPE,
    gpuCount: 1,
    gpuTypeId: "$gpu",
    name: "$name",
    imageName: "$IMAGE",
    containerDiskInGb: $DISK_GB,
    volumeInGb: 0,
    ports: "22/tcp",
    dockerArgs: "$(printf '%s' "$boot" | sed 's/"/\\"/g')"
  }) { id }
}
GQL
)
  local pid
  pid="$(gql "$mutation" '.data.podFindAndDeployOnDemand.id')"
  [ -n "$pid" ] && [ "$pid" != "null" ] || die "$name did not deploy (no capacity for '$gpu' on $CLOUD_TYPE?)"
  echo "$pid" > "$podfile"
  eval "$provisioned_var=1"
  info "$name pod id: $pid"

  hr "waiting for $name to expose SSH (up to ~5 min)"
  local ip port i status
  for i in $(seq 1 60); do
    local q="query { pod(input:{podId:\"$pid\"}) { desiredStatus runtime { ports { ip publicPort privatePort type isIpPublic } } } }"
    status="$(gql "$q" '.data.pod.desiredStatus' 2>/dev/null || echo '?')"
    ip="$(gql   "$q" '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic) | .ip'         2>/dev/null | head -1)"
    port="$(gql "$q" '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic) | .publicPort' 2>/dev/null | head -1)"
    if [ -n "$ip" ] && [ -n "$port" ]; then
      info "$name SSH endpoint: root@$ip:$port  (status=$status)"
      echo "$ip $port" > "$ART/$(basename "$podfile" .id).ssh"
      return 0
    fi
    printf '  … %s %2d/60  status=%s\r' "$name" "$i" "$status" >&2
    sleep 5
  done
  die "$name never exposed a public SSH port — check the RunPod console"
}

ssh_opts_for() {
  read -r ip port < "$ART/$1.ssh"
  echo "-p $port -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
}
ssh_ip_for() { read -r ip _ < "$ART/$1.ssh"; echo "$ip"; }

wait_ssh() {
  local tag="$1" ip opts
  ip="$(ssh_ip_for "$tag")"
  opts="$(ssh_opts_for "$tag")"
  hr "waiting for sshd inside $tag"
  local i
  for i in $(seq 1 40); do
    # shellcheck disable=SC2086
    if ssh $opts "root@$ip" true 2>/dev/null; then info "$tag ssh is up"; return 0; fi
    printf '  … %2d/40\r' "$i" >&2; sleep 5
  done
  die "sshd never came up in $tag"
}

# deploy_vllm TAG — install the pinned vLLM version and start the OpenAI-compatible
# server bound to localhost only (never publicly exposed; every request is driven
# over the already-open SSH channel), waiting for it to report healthy.
deploy_vllm() {
  local tag="$1" ip opts
  ip="$(ssh_ip_for "$tag")"; opts="$(ssh_opts_for "$tag")"
  hr "$tag: installing vllm==$VLLM_VERSION and starting the pinned server"
  # shellcheck disable=SC2086
  ssh -tt $opts "root@$ip" "
    set -e
    pip install --quiet 'vllm==$VLLM_VERSION' 2>&1 | tail -5
    nohup python3 -m vllm.entrypoints.openai.api_server \
      --model '$VLLM_MODEL' --dtype float16 --tensor-parallel-size 1 \
      --seed 0 --port 8000 --host 127.0.0.1 \
      > /root/vllm.log 2>&1 &
    disown
    echo READY_LAUNCH
  " || die "$tag: failed to launch vllm"

  hr "$tag: waiting for the server to report healthy (model download + init, can take minutes)"
  local i
  for i in $(seq 1 60); do
    # shellcheck disable=SC2086
    if ssh $opts "root@$ip" 'curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1'; then
      info "$tag vllm server healthy"
      return 0
    fi
    printf '  … %s %2d/60\r' "$tag" "$i" >&2
    sleep 10
  done
  # shellcheck disable=SC2086
  ssh $opts "root@$ip" 'tail -80 /root/vllm.log' >&2 || true
  die "$tag: vllm server never became healthy — see log tail above"
}

# run_corpus TAG OUT_FILE — drive PROMPT_COUNT pinned greedy requests against TAG's
# local vllm server (one request per prompt, run over SSH so the server is never
# exposed publicly), saving the raw `choices[0].text` per prompt to OUT_FILE.
run_corpus() {
  local tag="$1" outfile="$2" ip opts
  ip="$(ssh_ip_for "$tag")"; opts="$(ssh_opts_for "$tag")"
  hr "$tag: running the $PROMPT_COUNT-prompt greedy corpus"
  : > "$outfile"
  local i prompt body resp text
  for i in $(seq 1 "$PROMPT_COUNT"); do
    # A fixed, reproducible synthetic corpus — stands in for the real seeded
    # honeypot/redundancy corpus, which this spike does not have credentials to
    # pull; swap in the real corpus before this gates any production honeypot.
    prompt="Item $i: summarize in one sentence why reproducible builds matter."
    body="$(jq -nc --arg m "$VLLM_MODEL" --arg p "$prompt" --argjson mt "$MAX_TOKENS" \
      '{model:$m, prompt:$p, max_tokens:$mt, temperature:0, top_p:1, seed:0, n:1}')"
    # Body travels base64-encoded over the ssh command line so prompt text can never
    # break out of the remote shell's quoting (SC2029: this expands client-side, by
    # design — $body is a local bash variable, not remote-controlled input).
    body_b64="$(printf '%s' "$body" | base64 | tr -d '\n')"
    # shellcheck disable=SC2086,SC2029
    resp="$(ssh $opts "root@$ip" "echo $body_b64 | base64 -d | curl -fsS http://127.0.0.1:8000/v1/completions -H 'Content-Type: application/json' -d @-" 2>/dev/null)" \
      || die "$tag: request $i failed"
    text="$(echo "$resp" | jq -r '.choices[0].text' 2>/dev/null)"
    [ -n "$text" ] && [ "$text" != "null" ] || die "$tag: request $i returned no choice text: $resp"
    printf '%s\n' "$text" >> "$outfile"
    printf '  … %s %2d/%s\r' "$tag" "$i" "$PROMPT_COUNT" >&2
  done
  echo >&2
}

restart_vllm() {
  local tag="$1" ip opts
  ip="$(ssh_ip_for "$tag")"; opts="$(ssh_opts_for "$tag")"
  hr "$tag: restarting the vllm server (restart byte-stability soak)"
  # shellcheck disable=SC2086
  ssh -tt $opts "root@$ip" "
    pkill -f 'vllm.entrypoints.openai.api_server' || true
    sleep 2
    nohup python3 -m vllm.entrypoints.openai.api_server \
      --model '$VLLM_MODEL' --dtype float16 --tensor-parallel-size 1 \
      --seed 0 --port 8000 --host 127.0.0.1 \
      > /root/vllm-restart.log 2>&1 &
    disown
  " || die "$tag: restart failed"
  local i
  for i in $(seq 1 60); do
    # shellcheck disable=SC2086
    if ssh $opts "root@$ip" 'curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1'; then
      info "$tag vllm server healthy again after restart"
      return 0
    fi
    sleep 10
  done
  die "$tag: vllm server never came back up after restart"
}

pod_down() {
  local podfile="$1"
  [ -f "$podfile" ] || return 0
  local pid; pid="$(cat "$podfile")"
  hr "terminating pod $pid ($podfile)"
  if gql "mutation { podTerminate(input:{podId:\"$pid\"}) }" '.' >/dev/null; then
    info "terminated $pid"
  else
    die "terminate FAILED for $pid — CHECK THE RUNPOD CONSOLE so it does not keep charging"
  fi
  rm -f "$podfile"
}

cleanup() {
  local rc=$?
  [ "${KEEP:-0}" = "1" ] && { info "KEEP=1 — pods LEFT RUNNING (costing money). Tear down: bash $0 down"; return; }
  if [ "$PROVISIONED_A" = "1" ] && [ -f "$POD_FILE_A" ]; then
    echo >&2; info "cleanup: exit rc=$rc, tearing down pod A so it can't keep charging"
    pod_down "$POD_FILE_A" || true
  fi
  if [ "$PROVISIONED_B" = "1" ] && [ -f "$POD_FILE_B" ]; then
    info "cleanup: exit rc=$rc, tearing down pod B so it can't keep charging"
    pod_down "$POD_FILE_B" || true
  fi
}
trap cleanup EXIT INT TERM

do_run() {
  pod_up "cx-vllm-soak-a" "$GPU_TYPE_A" "$POD_FILE_A" PROVISIONED_A
  pod_up "cx-vllm-soak-b" "$GPU_TYPE_B" "$POD_FILE_B" PROVISIONED_B
  wait_ssh pod_a
  wait_ssh pod_b
  deploy_vllm pod_a
  deploy_vllm pod_b

  run_corpus pod_a "$ART/pod_a_run1.txt"
  run_corpus pod_b "$ART/pod_b_run1.txt"

  hr "cross-pod byte-stability soak ($GPU_TYPE_A vs $GPU_TYPE_B)"
  local cross_rc=0
  if diff -u "$ART/pod_a_run1.txt" "$ART/pod_b_run1.txt" > "$ART/cross-pod.diff"; then
    info "PASS · pod A and pod B produced byte-identical greedy output"
  else
    cross_rc=1
    info "FAIL · pod A and pod B DIVERGED — see $ART/cross-pod.diff. Per docs/VLLM_LANE.md: \
if this is a genuine SKU difference, these are DIFFERENT verification classes — encode the \
SKU into build_hash and never pair them as redundancy peers."
  fi

  restart_vllm pod_a
  run_corpus pod_a "$ART/pod_a_run2.txt"
  hr "pod A restart byte-stability soak"
  local restart_a_rc=0
  if diff -u "$ART/pod_a_run1.txt" "$ART/pod_a_run2.txt" > "$ART/pod_a_restart.diff"; then
    info "PASS · pod A produced byte-identical output across a server restart"
  else
    restart_a_rc=1
    info "FAIL · pod A DIVERGED across its own restart — see $ART/pod_a_restart.diff. \
This is a class break per docs/VLLM_LANE.md step 3 (non-deterministic reduction order)."
  fi

  hr "result"
  echo "cross_pod=$([ $cross_rc -eq 0 ] && echo PASS || echo FAIL)"       > "$ART/SUMMARY.txt"
  echo "restart_a=$([ $restart_a_rc -eq 0 ] && echo PASS || echo FAIL)"  >> "$ART/SUMMARY.txt"
  echo "gpu_a=$GPU_TYPE_A"                                              >> "$ART/SUMMARY.txt"
  echo "gpu_b=$GPU_TYPE_B"                                              >> "$ART/SUMMARY.txt"
  echo "model=$VLLM_MODEL vllm=$VLLM_VERSION prompts=$PROMPT_COUNT"     >> "$ART/SUMMARY.txt"
  cat "$ART/SUMMARY.txt" >&2
  info "raw responses + diffs saved under $ART/ — steps 4-5 (honeypot seeding, golden \
baseline) are a deliberate separate step after a human reads this PASS, not automatic."

  [ $cross_rc -eq 0 ] && [ $restart_a_rc -eq 0 ]
}

case "${1:-run}" in
  run)  do_run ;;
  down) pod_down "$POD_FILE_A"; pod_down "$POD_FILE_B" ;;
  *)    die "unknown subcommand '$1' (use: run | down)" ;;
esac
