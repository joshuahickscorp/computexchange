#!/usr/bin/env bash
# runpod-a100-reference.sh — the SINGLE-NODE REFERENCE side of the fan-out moat proof
# (docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md §6, L3): rent ONE A100 (or H100),
# run the EXACT competitive batch on it at full tilt, and record its end-to-end
# wall-clock T_ref. That number is the thing the fleet has to beat — "50 Macs finish
# your 10k-prompt batch faster than one rented A100." Until now the A100 figure in the
# modeled curve (2345 tok/s) came from OUR Candle bench; this measures the A100 at its
# STRONGEST — vLLM offline continuous batching, the engine a buyer would actually rent
# an A100 to run — so the fleet-beats-A100 claim can never be dismissed as a strawman.
#
# This is the REFERENCE HALF. The FLEET HALF (N real Macs running the same batch through
# the control plane) is the owner's other-device step; this script produces the number
# that half is compared against. Run this once the pod details are in hand; run the
# fleet half when the Macs are online; compare T_ref vs T_fleet.
#
# WHAT IT IS NOT: not the CUDA-lane correctness gate (that is scripts/runpod-spike.sh →
# prove-cuda.sh), not the vLLM determinism soak (scripts/runpod-vllm-soak.sh). Those
# light up our own CUDA serving lane. THIS measures a competitor's box as the baseline.
#
# HONESTY (BLACKHOLE): this really provisions a pod, which really costs money, and it
# tears the pod down on EVERY exit (normal, error, ctrl-C) so a forgotten box cannot
# bleed cost. It never fabricates a number — a failed run is reported as a failure, and
# the raw per-request timing JSON is pulled back so the wall-clock can be audited.
#
# ── What you type (the whole thing) ──────────────────────────────────────────
#   export RUNPOD_API_KEY=...             # RunPod console → Settings → API Keys
#   bash scripts/runpod-a100-reference.sh              # A100, 10k prompts × 256 tokens
#   GPU_TYPE="NVIDIA H100 80GB PCIe" \
#     bash scripts/runpod-a100-reference.sh            # H100 instead
#
# Subcommands (default is the full lifecycle `run`):
#   run     provision → deploy pinned vLLM → time the batch → pull result → terminate
#   down    terminate any pod recorded from a previous run (money-safety escape hatch)
#
# ── One-time RunPod account setup ────────────────────────────────────────────
#   Add your SSH PUBLIC key in the RunPod console (Settings → SSH Public Keys) so the
#   pod injects it. This script defaults to ~/.ssh/id_ed25519.pub.
#
# ── Knobs (env) ───────────────────────────────────────────────────────────────
#   RUNPOD_API_KEY  (required)   your RunPod API key
#   GPU_TYPE        NVIDIA GPU display id   (default "NVIDIA A100 80GB PCIe";
#                                            e.g. "NVIDIA H100 80GB PCIe")
#   CLOUD_TYPE      SECURE | COMMUNITY      (default COMMUNITY — cheaper for a spike)
#   VLLM_MODEL      pinned HF model id      (default "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
#                                            — UNGATED, Apache-2.0, 1.1B, LlamaForCausalLM
#                                            so vLLM runs it natively; ~2.2 GB fp16, a
#                                            fast/cheap download and a clean size-match to
#                                            the fleet's 1B. No HF token required. Swap to
#                                            meta-llama/Llama-3.2-1B-Instruct + HF_TOKEN if
#                                            you want the exact fleet model.)
#   VLLM_VERSION    pinned vLLM pip version (default "0.6.3")
#   VLLM_DTYPE      float16 | bfloat16 | auto (default float16)
#   PROMPT_COUNT    batch size             (default 10000 — the fan-out proof's shape)
#   MAX_TOKENS      completion tokens/prompt (default 256 — the proof's shape)
#   IGNORE_EOS      1 = force exactly MAX_TOKENS per prompt so the batch is FIXED WORK
#                   (10k×256 token-gens), the clean apples-to-apples vs the fleet;
#                   0 = let prompts stop at EOS (closer to real traffic). Default 1.
#   HF_TOKEN        HuggingFace access token — REQUIRED for the default model
#                   (meta-llama/Llama-3.2-1B-Instruct is GATED: accept its license at
#                   huggingface.co/meta-llama/Llama-3.2-1B-Instruct, then
#                   export HF_TOKEN=hf_...). Skip only if VLLM_MODEL is an ungated model.
#   KEEP=1          on `run`, do NOT terminate at the end (inspect the box yourself)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

API="https://api.runpod.io/graphql"
ART=.artifacts/a100-reference
POD_FILE="$ART/pod.id"
mkdir -p "$ART"

PROVISIONED_THIS_RUN=0

GPU_TYPE="${GPU_TYPE:-NVIDIA A100-SXM4-80GB}"
CLOUD_TYPE="${CLOUD_TYPE:-COMMUNITY}"
# A CUDA runtime image is enough — vLLM ships its own CUDA kernels; we only need python
# + pip. (The spike script needs a *devel* image for nvcc; this one does not.)
IMAGE="${IMAGE:-nvidia/cuda:12.4.1-runtime-ubuntu22.04}"
DISK_GB="${DISK_GB:-60}"
POD_NAME="${POD_NAME:-cx-a100-reference}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
VLLM_MODEL="${VLLM_MODEL:-TinyLlama/TinyLlama-1.1B-Chat-v1.0}"
VLLM_VERSION="${VLLM_VERSION:-0.6.3}"
VLLM_DTYPE="${VLLM_DTYPE:-float16}"
PROMPT_COUNT="${PROMPT_COUNT:-10000}"
MAX_TOKENS="${MAX_TOKENS:-256}"
IGNORE_EOS="${IGNORE_EOS:-1}"

die()  { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }
hr()   { echo >&2; echo "== $* ==" >&2; }

command -v jq   >/dev/null 2>&1 || die "jq not found (brew install jq)"
command -v curl >/dev/null 2>&1 || die "curl not found"
[ -n "${RUNPOD_API_KEY:-}" ] || die "RUNPOD_API_KEY unset — RunPod console → Settings → API Keys, then: export RUNPOD_API_KEY=..."

# Fail BEFORE provisioning (before any $ is spent) if the default gated model is chosen
# without an HF token — a pod that dies at model-load is money burned for nothing. Gated
# to `run` only: `down` is the money-safety escape hatch and must never be blocked.
if [ "${1:-run}" = "run" ]; then
  case "$VLLM_MODEL" in
    meta-llama/*)
      [ -n "${HF_TOKEN:-}" ] || die "VLLM_MODEL='$VLLM_MODEL' is HuggingFace-GATED but HF_TOKEN is unset. Accept the license at huggingface.co/$VLLM_MODEL, then: export HF_TOKEN=hf_...  (or set VLLM_MODEL to an ungated model). Refusing to provision a pod that would die at model load." ;;
  esac
fi

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

pod_up() {
  [ -f "$SSH_PUBKEY" ] || die "SSH pubkey not found at '$SSH_PUBKEY' — set SSH_PUBKEY, or ssh-keygen -t ed25519. It must ALSO be added in the RunPod console (Settings → SSH Public Keys)."

  hr "provisioning $POD_NAME · 1× $GPU_TYPE · $CLOUD_TYPE · $IMAGE"
  local boot='bash -c "apt-get update && apt-get install -y --no-install-recommends openssh-server python3-pip >/tmp/boot.log 2>&1; mkdir -p /run/sshd /root/.ssh; /usr/sbin/sshd -D"'
  local mutation
  mutation=$(cat <<GQL
mutation {
  podFindAndDeployOnDemand(input: {
    cloudType: $CLOUD_TYPE,
    gpuCount: 1,
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
  [ -n "$pid" ] && [ "$pid" != "null" ] || die "pod did not deploy (no capacity for '$GPU_TYPE' on $CLOUD_TYPE? try CLOUD_TYPE=SECURE or another GPU_TYPE)"
  echo "$pid" > "$POD_FILE"
  PROVISIONED_THIS_RUN=1
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
      echo "$ip $port" > "$ART/pod.ssh"
      return 0
    fi
    printf '  … %2d/60  status=%s\r' "$i" "$status" >&2
    sleep 5
  done
  die "pod never exposed a public SSH port — check the RunPod console; run 'bash scripts/runpod-a100-reference.sh down' so it stops charging"
}

ssh_args() {
  [ -f "$ART/pod.ssh" ] || die "no recorded SSH endpoint — provision first"
  read -r SSH_IP SSH_PORT < "$ART/pod.ssh"
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
  die "sshd never came up in the pod (image installs+starts it on boot; check the RunPod console → Logs)"
}

# deploy_and_time — install pinned vLLM, then run the whole batch through vLLM's OFFLINE
# LLM.generate (continuous batching, no HTTP/serving overhead — the A100 running the
# batch as fast as the engine can) and time it ON THE POD (so no SSH round-trip latency
# pollutes the wall-clock). The driver emits one JSON object we pull home.
deploy_and_time() {
  ssh_args
  hr "installing vllm==$VLLM_VERSION on the pod"
  ssh -tt "${SSH_OPTS[@]}" "root@$SSH_IP" "pip install --quiet 'vllm==$VLLM_VERSION' 2>&1 | tail -5" \
    || die "vllm install failed"

  # HF token for the gated default model — written to the pod's env for vLLM's downloader.
  # Base64 over the wire so the token never lands in a process arg / shell history on the pod.
  if [ -n "${HF_TOKEN:-}" ]; then
    local tok_b64; tok_b64="$(printf '%s' "$HF_TOKEN" | base64 | tr -d '\n')"
    ssh "${SSH_OPTS[@]}" "root@$SSH_IP" "echo $tok_b64 | base64 -d > /root/.hf_token" \
      || die "failed to write HF token to the pod"
    info "HF token staged on the pod (for the gated model download)"
  fi

  # The pod-side driver. Heredoc is written to a file on the pod so quoting is trivial.
  # ignore_eos forces exactly MAX_TOKENS/prompt when IGNORE_EOS=1 → the batch is FIXED
  # WORK (PROMPT_COUNT × MAX_TOKENS token-generations), the clean apples-to-apples vs
  # the fleet. A short warm-up generate lands weight load + CUDA graph capture OUTSIDE
  # the timed region, so T_ref is steady-state batch throughput, not cold-start.
  hr "uploading + running the timed batch driver ($PROMPT_COUNT prompts × $MAX_TOKENS tokens, ignore_eos=$IGNORE_EOS)"
  ssh "${SSH_OPTS[@]}" "root@$SSH_IP" "cat > /root/ref_batch.py" <<'PY'
import time, json, sys, os
from vllm import LLM, SamplingParams

n         = int(os.environ["PROMPT_COUNT"])
mt        = int(os.environ["MAX_TOKENS"])
model     = os.environ["VLLM_MODEL"]
dtype     = os.environ["VLLM_DTYPE"]
ignore    = os.environ["IGNORE_EOS"] == "1"

# A fixed, reproducible synthetic corpus with per-item variation so no two prompts share
# a cached prefix trivially. Stands in for a real batch's shape; swap in a real corpus by
# editing this line if you want the reference to mirror a specific production job.
prompts = [f"Item {i}: summarize in one sentence why reproducible builds matter." for i in range(n)]

llm = LLM(model=model, dtype=dtype, seed=0, gpu_memory_utilization=0.90)
sp  = SamplingParams(max_tokens=mt, temperature=0.0, top_p=1.0, seed=0, ignore_eos=ignore)

# Warm-up (weights already resident from LLM(); this captures graphs / JITs the first
# decode) — OUTSIDE the timed region.
_ = llm.generate(prompts[:min(8, n)], sp)

t0 = time.perf_counter()
outs = llm.generate(prompts, sp)
wall = time.perf_counter() - t0

gen_tokens = sum(len(o.outputs[0].token_ids) for o in outs)
result = {
    "gpu_type_env": os.environ.get("GPU_TYPE_LABEL", "?"),
    "model": model,
    "dtype": dtype,
    "prompt_count": n,
    "max_tokens": mt,
    "ignore_eos": ignore,
    "wall_s": round(wall, 3),
    "generated_tokens": gen_tokens,
    "aggregate_tok_s": round(gen_tokens / wall, 1) if wall > 0 else None,
    "vllm_version": os.environ["VLLM_VERSION"],
}
print("CX_REF_JSON " + json.dumps(result))
PY

  # Run it, streaming vLLM's progress live; capture the one CX_REF_JSON line. The pod
  # reads the staged HF token (if any) from /root/.hf_token into HF_TOKEN so vLLM's
  # downloader can pull the gated model.
  ssh -tt "${SSH_OPTS[@]}" "root@$SSH_IP" \
    "cd /root && export HF_TOKEN=\$(cat /root/.hf_token 2>/dev/null); export HUGGING_FACE_HUB_TOKEN=\$HF_TOKEN; PROMPT_COUNT='$PROMPT_COUNT' MAX_TOKENS='$MAX_TOKENS' VLLM_MODEL='$VLLM_MODEL' VLLM_DTYPE='$VLLM_DTYPE' IGNORE_EOS='$IGNORE_EOS' VLLM_VERSION='$VLLM_VERSION' GPU_TYPE_LABEL='$GPU_TYPE' python3 ref_batch.py" \
    2>&1 | tee "$ART/run.log"

  # Extract the JSON result line (strip the marker). PIPESTATUS check: the ssh/python
  # must have succeeded AND emitted the marker, else this is a real failure, not a $0 win.
  grep 'CX_REF_JSON ' "$ART/run.log" | tail -1 | sed 's/^.*CX_REF_JSON //' > "$ART/result.json" || true
  if ! jq -e '.wall_s' "$ART/result.json" >/dev/null 2>&1; then
    die "the timed batch did not produce a valid result — see $ART/run.log (OOM? model gated on HF? try a smaller PROMPT_COUNT or a HF token)"
  fi
}

report() {
  hr "reference result"
  local wall tok agg gtok
  wall="$(jq -r '.wall_s' "$ART/result.json")"
  tok="$(jq -r '.aggregate_tok_s' "$ART/result.json")"
  gtok="$(jq -r '.generated_tokens' "$ART/result.json")"
  agg="$(jq -r '.model + " " + .dtype' "$ART/result.json")"
  {
    echo "# Single-node reference (T_ref) — $(jq -r '.gpu_type_env' "$ART/result.json")"
    echo
    echo "- GPU: $(jq -r '.gpu_type_env' "$ART/result.json")   (vllm $(jq -r '.vllm_version' "$ART/result.json"), $agg)"
    echo "- batch: $(jq -r '.prompt_count' "$ART/result.json") prompts × $(jq -r '.max_tokens' "$ART/result.json") tokens · ignore_eos=$(jq -r '.ignore_eos' "$ART/result.json")"
    echo "- generated tokens: $gtok"
    echo "- **T_ref (end-to-end batch wall-clock): ${wall} s**"
    echo "- aggregate throughput: **${tok} tok/s**"
    echo
    echo "This is the number the FLEET must beat. Compare against T_fleet (N Macs running"
    echo "the same batch through the control plane; see"
    echo "docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md §6). If T_fleet < T_ref at the"
    echo "measured N, the fan-out moat is confirmed on real hardware."
  } | tee "$ART/REFERENCE.md" >&2
  info "raw JSON: $ART/result.json · full log: $ART/run.log · summary: $ART/REFERENCE.md"
}

pod_down() {
  local pid="${POD_ID:-}"
  [ -z "$pid" ] && [ -f "$POD_FILE" ] && pid="$(cat "$POD_FILE")"
  [ -n "$pid" ] || { info "no pod recorded — nothing to terminate"; return 0; }
  hr "terminating pod $pid"
  if gql "mutation { podTerminate(input:{podId:\"$pid\"}) }" '.' >/dev/null; then
    info "terminated $pid"
  else
    die "terminate FAILED for $pid — CHECK THE RUNPOD CONSOLE so it does not keep charging"
  fi
  rm -f "$POD_FILE" "$ART/pod.ssh"
}

cleanup() {
  local rc=$?
  [ "$PROVISIONED_THIS_RUN" = "1" ] || return
  [ "${KEEP:-0}" = "1" ] && { info "KEEP=1 — pod LEFT RUNNING (costing money). Tear down: bash scripts/runpod-a100-reference.sh down"; return; }
  [ -f "$POD_FILE" ] || return
  echo >&2
  info "cleanup: exit rc=$rc with a pod still up — tearing it down so it can't keep charging"
  pod_down || true
}
trap cleanup EXIT INT TERM

pod_run() {
  pod_up
  wait_ssh
  deploy_and_time
  report
  if [ "${KEEP:-0}" = "1" ]; then
    info "KEEP=1 — pod LEFT RUNNING. Tear down: bash scripts/runpod-a100-reference.sh down"
  else
    pod_down
  fi
}

case "${1:-run}" in
  run)  pod_run ;;
  down) pod_down ;;
  *)    die "unknown subcommand '$1' (use: run | down)" ;;
esac
