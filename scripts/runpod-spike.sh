#!/usr/bin/env bash
# runpod-spike.sh — one-command DEEP NVIDIA/CUDA validation on a real RunPod GPU box.
#
# The June 2026 spike (docs/internal/RUNPOD_SPIKE.md, archived) validated the OLD
# Candle CUDA build with a smoke test. This does TWO things on a freshly-provisioned
# pod against the CURRENT branch:
#   1. scripts/prove-cuda.sh   — the correctness GATE (builds --features cuda,
#      self-classifies nvidia_*, proves batched==serial, sandbox GPU reachability);
#   2. scripts/gpu-benchmark.sh — the DEEP capability SWEEP (batch-throughput curve
#      across models + batch sizes up to 64, raw FP32/FP16/TF32 TFLOPS + bandwidth),
#      producing a REPORT.md pulled back to this Mac to sit beside the local Metal run.
# Together: proof the CUDA lane is honest AND numbers showing how strong it is — the
# evidence behind "best on CUDA and Apple Silicon". A ~$6 A100 budget is ~3-5h of
# runtime; the whole run is well under an hour, so it costs about a dollar.
# (This is still NOT the vLLM determinism soak — that needs pinned reference servers,
# see docs/VLLM_LANE.md — it is the bare-Candle CUDA capability run.)
#
# HONESTY (BLACKHOLE): this really provisions a pod, which really costs money, and it
# tears the pod down on exit (even on failure) so a forgotten box does not bleed cost.
# It fails loudly if a credential or tool is missing and never pretends a pod ran.
#
# ── What you type (the whole thing) ──────────────────────────────────────────
#   export RUNPOD_API_KEY=...            # RunPod console → Settings → API Keys
#   bash scripts/runpod-spike.sh          # up → rsync → prove-cuda + benchmark → pull report → down
#
# Subcommands (default is the full lifecycle `run`):
#   run     provision → SSH → rsync → prove-cuda.sh + gpu-benchmark.sh → pull report → terminate
#   up      provision + wait for SSH only, print the ssh line, leave it running
#   down    terminate the pod recorded in .artifacts/runpod-spike.pod (or $POD_ID)
#   ssh     open an interactive shell to the running pod
#
# ── One-time RunPod account setup (why: how the pod lets you in) ──────────────
#   Add your SSH PUBLIC key in the RunPod console (Settings → SSH Public Keys).
#   RunPod injects it into every pod, which is how this script (and you) SSH in.
#   Point SSH_PUBKEY below at the matching public key if it is not ~/.ssh/id_ed25519.pub.
#
# ── Knobs (env) ──────────────────────────────────────────────────────────────
#   RUNPOD_API_KEY  (required)   your RunPod API key
#   GPU_TYPE        NVIDIA GPU display id  (default "NVIDIA A100 80GB PCIe")
#   GPU_COUNT       number of GPUs         (default 1)
#   CLOUD_TYPE      SECURE | COMMUNITY     (default COMMUNITY — cheaper for a spike)
#   IMAGE           container image        (default a CUDA-12.4 devel image)
#   DISK_GB         container disk         (default 40)
#   POD_NAME        display name           (default cx-cuda-spike)
#   SSH_PUBKEY      path to your ssh pubkey (default ~/.ssh/id_ed25519.pub)
#   KEEP=1          on `run`, do NOT terminate at the end (inspect the box yourself)
#   BENCH_MODELS       models the deep sweep runs (default llama-3.2-1b + qwen2.5-7b)
#   BENCH_BATCH_SIZES  batch sizes swept        (default 1,2,4,8,16,32,64)
#   BENCH_MAX_TOKENS   decode length per request(default 96)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

API="https://api.runpod.io/graphql"
ART=.artifacts
POD_FILE="$ART/runpod-spike.pod"
mkdir -p "$ART"

# Money-safety state (see the EXIT trap below). PROVISIONED_THIS_RUN flips to 1 the
# instant pod_up reserves a (billing) pod, so the trap only ever tears down what THIS
# invocation created — never a pod left up by a prior `up`. LEAVE_RUNNING is set by the
# `up` subcommand, which intentionally leaves the pod running.
PROVISIONED_THIS_RUN=0
LEAVE_RUNNING=0

GPU_TYPE="${GPU_TYPE:-NVIDIA A100 80GB PCIe}"
GPU_COUNT="${GPU_COUNT:-1}"
CLOUD_TYPE="${CLOUD_TYPE:-COMMUNITY}"
# A CUDA *devel* image so nvcc is present (prove-cuda.sh compiles gpubench.cu). The
# runtime-only images lack nvcc. Ubuntu 22.04 + CUDA 12.4 matches the SM arch logic.
IMAGE="${IMAGE:-nvidia/cuda:12.4.1-devel-ubuntu22.04}"
DISK_GB="${DISK_GB:-40}"
POD_NAME="${POD_NAME:-cx-cuda-spike}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"

die()  { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }
hr()   { echo >&2; echo "== $* ==" >&2; }

command -v jq   >/dev/null 2>&1 || die "jq not found (brew install jq) — needed to parse the RunPod API"
command -v curl >/dev/null 2>&1 || die "curl not found"
[ -n "${RUNPOD_API_KEY:-}" ]     || die "RUNPOD_API_KEY unset — RunPod console → Settings → API Keys, then: export RUNPOD_API_KEY=..."

# gql QUERY [JQ_FILTER] — POST a GraphQL query, fail loudly on a GraphQL error, and
# optionally project the data with jq. The api key rides the URL query param, which
# is how RunPod's GraphQL endpoint authenticates.
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

# ── provision + poll for the public SSH endpoint ─────────────────────────────
pod_up() {
  [ -f "$SSH_PUBKEY" ] || die "SSH pubkey not found at '$SSH_PUBKEY' — set SSH_PUBKEY, or ssh-keygen -t ed25519. It must ALSO be added in the RunPod console (Settings → SSH Public Keys) so the pod injects it."

  hr "provisioning $POD_NAME · $GPU_COUNT× $GPU_TYPE · $CLOUD_TYPE · $IMAGE"
  # podFindAndDeployOnDemand reserves an on-demand pod. We request port 22 (TCP) so
  # RunPod maps a PUBLIC ip:port for SSH, and start sshd from the image's args (the
  # base CUDA image has no sshd running by default, so install+launch it on boot).
  local boot='bash -c "apt-get update && apt-get install -y --no-install-recommends openssh-server rsync git curl >/tmp/boot.log 2>&1; mkdir -p /run/sshd /root/.ssh; grep -q runpod /root/.ssh/authorized_keys 2>/dev/null || true; /usr/sbin/sshd -D"'
  local mutation
  mutation=$(cat <<GQL
mutation {
  podFindAndDeployOnDemand(input: {
    cloudType: $CLOUD_TYPE,
    gpuCount: $GPU_COUNT,
    gpuTypeId: "$GPU_TYPE",
    name: "$POD_NAME",
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
  [ -n "$pid" ] && [ "$pid" != "null" ] || die "pod did not deploy (no capacity for '$GPU_TYPE' on $CLOUD_TYPE? try GPU_TYPE=... or CLOUD_TYPE=SECURE)"
  echo "$pid" > "$POD_FILE"
  PROVISIONED_THIS_RUN=1   # from here on, the EXIT trap owns teardown
  info "pod id: $pid  (recorded in $POD_FILE)"

  hr "waiting for the pod to expose SSH (up to ~5 min)"
  local ip port i status
  for i in $(seq 1 60); do
    local q="query { pod(input:{podId:\"$pid\"}) { desiredStatus runtime { ports { ip publicPort privatePort type isIpPublic } } } }"
    status="$(gql "$q" '.data.pod.desiredStatus' 2>/dev/null || echo '?')"
    ip="$(gql   "$q" '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic) | .ip'         2>/dev/null | head -1)"
    port="$(gql "$q" '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic) | .publicPort' 2>/dev/null | head -1)"
    if [ -n "$ip" ] && [ -n "$port" ]; then
      info "SSH endpoint: root@$ip:$port  (status=$status)"
      echo "$ip $port" > "$ART/runpod-spike.ssh"
      return 0
    fi
    printf '  … %2d/60  status=%s\r' "$i" "$status" >&2
    sleep 5
  done
  die "pod never exposed a public SSH port — check the RunPod console; run 'bash scripts/runpod-spike.sh down' to avoid paying for it"
}

ssh_args() {
  [ -f "$ART/runpod-spike.ssh" ] || die "no recorded SSH endpoint — run 'up' first"
  read -r SSH_IP SSH_PORT < "$ART/runpod-spike.ssh"
  # StrictHostKeyChecking=accept-new: a fresh throwaway box has an unknown host key
  # every time; we accept it once rather than wedge on the prompt. Not a security
  # regression for an ephemeral spike box we provisioned seconds ago.
  SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)
}

wait_ssh() {
  ssh_args
  hr "waiting for sshd inside the pod"
  local i
  for i in $(seq 1 40); do
    if ssh "${SSH_OPTS[@]}" "root@$SSH_IP" true 2>/dev/null; then info "ssh is up"; return 0; fi
    printf '  … %2d/40\r' "$i" >&2; sleep 5
  done
  die "sshd never came up in the pod (the image installs+starts it on boot; check the RunPod web console → Logs)"
}

pod_run() {
  pod_up
  wait_ssh
  ssh_args

  hr "rsync repo → pod (code, not target/.git — the pod rebuilds)"
  # Mirror the prod pattern (rsync from the Mac). Exclude the heavy/irrelevant dirs
  # so the upload is code only; the pod builds --features cuda from source.
  rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.git/' --exclude 'agent/target/' --exclude 'control/control' \
    --exclude '.artifacts/' --exclude 'macapp/.build/' --exclude 'node_modules/' \
    --exclude '*.log' \
    ./ "root@$SSH_IP:/root/cx/" \
    || die "rsync failed"

  hr "1/2 · scripts/prove-cuda.sh — CUDA correctness gate (builds + parity, a few min)"
  # -tt forces a pty so the remote script's progress streams live.
  ssh -tt "${SSH_OPTS[@]}" "root@$SSH_IP" \
    'cd /root/cx && bash scripts/prove-cuda.sh' 2>&1 | tee "$ART/runpod-spike-proof.log"
  local rc=${PIPESTATUS[0]}

  hr "2/2 · scripts/gpu-benchmark.sh — DEEP capability sweep (the $ headroom buys this)"
  # The correctness gate above proves the lane is honest; this measures HOW GOOD it is
  # — the batch-throughput sweep + raw compute that make the "best on CUDA" claim
  # evidence-backed. SKIP_BUILD=1 reuses prove-cuda's --features cuda binary. BENCH_*
  # override the sweep depth; defaults below use the big-VRAM headroom (7B + B up to 64).
  local bench_models="${BENCH_MODELS:-llama-3.2-1b-instruct-q4,qwen2.5-7b-instruct-q4}"
  local bench_batches="${BENCH_BATCH_SIZES:-1,2,4,8,16,32,64}"
  local bench_tokens="${BENCH_MAX_TOKENS:-96}"
  ssh -tt "${SSH_OPTS[@]}" "root@$SSH_IP" \
    "cd /root/cx && SKIP_BUILD=1 MODELS='$bench_models' BATCH_SIZES='$bench_batches' MAX_TOKENS='$bench_tokens' bash scripts/gpu-benchmark.sh" \
    2>&1 | tee "$ART/runpod-spike-bench.log"
  local brc=${PIPESTATUS[0]}

  hr "pull the benchmark report bundle back to this Mac"
  # Bring the JSON + REPORT.md home so the CUDA numbers sit next to the Metal run for a
  # side-by-side. Non-fatal: a pull hiccup must not strand a running (costing) pod.
  mkdir -p "$ART/gpu-bench"
  if rsync -az -e "ssh ${SSH_OPTS[*]}" "root@$SSH_IP:/root/cx/.artifacts/gpu-bench/" "$ART/gpu-bench/" 2>/dev/null; then
    info "report bundle pulled → $ART/gpu-bench/ (diff against your local Metal run)"
  else
    info "could not pull the report bundle (see it on the pod at /root/cx/.artifacts/gpu-bench/)"
  fi

  hr "result"
  if [ "$rc" -eq 0 ]; then
    info "CUDA lane PROVEN (correctness) on $(cat "$POD_FILE")"
  else
    info "prove-cuda.sh reported failures (rc=$rc) · see $ART/runpod-spike-proof.log"
  fi
  if [ "$brc" -eq 0 ]; then
    info "deep benchmark OK · REPORT.md in $ART/gpu-bench/  · log: $ART/runpod-spike-bench.log"
  else
    info "benchmark reported failures (rc=$brc) · see $ART/runpod-spike-bench.log"
  fi

  if [ "${KEEP:-0}" = "1" ]; then
    ssh_args
    info "KEEP=1 — pod LEFT RUNNING (it is costing money). Shell in: bash scripts/runpod-spike.sh ssh · Tear down: bash scripts/runpod-spike.sh down"
  else
    pod_down
  fi
  # Overall rc: correctness gate is load-bearing; the benchmark is evidence. Fail if
  # either failed, so a harness/CI treats a diverged decode or a broken bench as red.
  [ "$rc" -eq 0 ] && [ "$brc" -eq 0 ] && return 0 || return 1
}

pod_down() {
  local pid="${POD_ID:-}"
  [ -z "$pid" ] && [ -f "$POD_FILE" ] && pid="$(cat "$POD_FILE")"
  [ -n "$pid" ] || { info "no pod recorded ($POD_FILE absent and POD_ID unset) — nothing to terminate"; return 0; }
  hr "terminating pod $pid"
  if gql "mutation { podTerminate(input:{podId:\"$pid\"}) }" '.' >/dev/null; then
    info "terminated $pid"
  else
    die "terminate FAILED for $pid — CHECK THE RUNPOD CONSOLE so it does not keep charging"
  fi
  rm -f "$POD_FILE" "$ART/runpod-spike.ssh"
}

pod_shell() {
  ssh_args
  read -r SSH_IP SSH_PORT < "$ART/runpod-spike.ssh"
  info "ssh root@$SSH_IP -p $SSH_PORT"
  ssh "${SSH_OPTS[@]}" "root@$SSH_IP"
}

# EXIT trap — the actual money-safety guarantee. It fires on EVERY exit of THIS
# process (normal, `die`, unbound-var, SIGINT/SIGTERM), so a pod this run provisioned
# is always torn down — closing every `die`-before-teardown leak (a slow SSH port, a
# dead sshd, a failed rsync, a mid-run ctrl-C). It NO-OPs when: we did not provision
# (PROVISIONED_THIS_RUN=0, e.g. `ssh`/`down`), the caller wants it kept (KEEP=1 or the
# `up` subcommand sets LEAVE_RUNNING=1), or pod_down already ran (POD_FILE removed).
cleanup() {
  local rc=$?
  [ "$PROVISIONED_THIS_RUN" = "1" ] || return
  [ "$LEAVE_RUNNING" = "1" ] && return
  [ "${KEEP:-0}" = "1" ] && return
  [ -f "$POD_FILE" ] || return   # already torn down on the happy path
  echo >&2
  info "cleanup: exit rc=$rc with a pod still up — tearing it down so it can't keep charging"
  pod_down || true
}
trap cleanup EXIT INT TERM

case "${1:-run}" in
  run)  pod_run ;;
  # `up` intentionally leaves the pod running; tell the trap not to reap it.
  up)   pod_up; wait_ssh; ssh_args; LEAVE_RUNNING=1; info "pod up. Shell: bash scripts/runpod-spike.sh ssh · Prove: (up then) bash scripts/runpod-spike.sh run · Down: bash scripts/runpod-spike.sh down" ;;
  down) pod_down ;;
  ssh)  pod_shell ;;
  *)    die "unknown subcommand '$1' (use: run | up | down | ssh)" ;;
esac
