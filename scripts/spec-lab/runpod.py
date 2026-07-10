#!/usr/bin/env python3
"""
runpod.py — money-safe RunPod GPU lifecycle for the spec-lab experiment engine.

The safety contract (nothing here is optional):
  1. Every pod this module creates is TRACKED in a state file on disk the instant
     it is created, BEFORE anything else, so a crash can never orphan a billing pod.
  2. teardown() and terminate_all_tracked() are idempotent and are wired to
     atexit + SIGINT/SIGTERM by register_cleanup(), so any exit path tears pods down.
  3. A hard wall-clock DEADLINE (max_minutes) is enforced by the orchestrator; this
     module exposes terminate_all_tracked() as the watchdog's teardown call.
  4. provision_reachable() only returns a pod this machine can actually SSH to — it
     PROVES reachability (a real SSH round-trip) before returning, and terminates any
     pod that provisions but is unreachable (the real-world failure mode: RunPod hands
     out pods in datacenters this network cannot route to). No silent billing.

This module is import-safe (no side effects on import) and has zero third-party deps
(urllib + subprocess only), so the orchestrator stays a single `python3` invocation.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import atexit

API = "https://api.runpod.io/graphql"
STATE_FILE = os.environ.get(
    "SPEC_LAB_POD_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tracked_pods.json"),
)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_RUNPOD_ENV_FILE = os.path.join(REPO_ROOT, ".secrets", "runpod.env")
KEY_PATH = os.path.expanduser(os.environ.get("SPEC_LAB_SSH_KEY", "~/.ssh/id_ed25519"))
PUBKEY_PATH = KEY_PATH + ".pub"

SSH_OPTS = [
    "-4", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=20",
    # ServerAliveCountMax raised from the default 3 so a transient stall tolerates ~120s
    # of silence before the client gives up. NOTE: this only mitigates idle/NAT-timeout
    # drops; it does NOT survive a peer-side connection RESET on a long (~1hr) render SSH
    # (2026-07-09: a 49-min 4K render SSH was reset mid-stream). The real fix for heavy
    # renders is detached-on-pod + poll — implemented as ssh_detached() below.
    "-o", "ServerAliveCountMax=6",
    "-i", KEY_PATH,
]


def redact_secret(text):
    value = str(text)
    key = os.environ.get("RUNPOD_API_KEY", "").strip() or _api_key_from_env_file()
    if key:
        value = value.replace(key, "[REDACTED_RUNPOD_API_KEY]")
    value = re.sub(r"api_key=[^&\s'\"]+", "api_key=[REDACTED]", value)
    value = re.sub(r"Bearer\s+[A-Za-z0-9_.-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"rpa_[A-Za-z0-9]+", "rpa_[REDACTED]", value)
    return value


def _api_key():
    k = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not k:
        k = _api_key_from_env_file()
    if not k:
        raise SystemExit(
            "RUNPOD_API_KEY unset — export it or create .secrets/runpod.env before running the lab."
        )
    return k


def _api_key_from_env_file():
    path = os.path.expanduser(os.environ.get("CX_RUNPOD_ENV_FILE", DEFAULT_RUNPOD_ENV_FILE))
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "RUNPOD_API_KEY":
                    return value.strip().strip("'\"")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return ""


def gql(query, variables=None, retries=3):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    # RunPod's WAF 403s the default Python-urllib User-Agent; send a curl-like UA
    # and the key BOTH as a query param and a Bearer header (belt + suspenders).
    key = _api_key()
    req = urllib.request.Request(
        f"{API}?api_key={key}", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "curl/8.4.0",
                 "Authorization": f"Bearer {key}"})
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                out = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_error = e
            if attempt >= retries:
                raise
            sleep_s = min(2 ** attempt, 8)
            print(
                f"[runpod] API transient HTTP {e.code}; retrying in {sleep_s}s",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            if attempt >= retries:
                raise
            sleep_s = min(2 ** attempt, 8)
            print(
                f"[runpod] API transient error; retrying in {sleep_s}s: {e}",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
    else:
        raise last_error
    if out.get("errors"):
        raise RuntimeError("RunPod API error: " + json.dumps(out["errors"]))
    return out["data"]


# ---- tracked-pod state (the anti-orphan ledger) --------------------------------

def _load_tracked():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_tracked(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(set(ids)), f)


def _track(pod_id):
    ids = _load_tracked()
    ids.append(pod_id)
    _save_tracked(ids)


def _untrack(pod_id):
    _save_tracked([p for p in _load_tracked() if p != pod_id])


# ---- lifecycle -----------------------------------------------------------------

def terminate(pod_id):
    try:
        gql('mutation($i:String!){ podTerminate(input:{podId:$i}) }', {"i": pod_id})
    except Exception as e:
        msg = str(e)
        # POD_NOT_FOUND is a DEFINITIVE "this pod id is not on the account" — it was never
        # created, already stopped, or auto-removed. That is by definition NOT an orphan, so
        # treat it as a successful teardown: untrack it and return True. Otherwise a phantom
        # id (e.g. a pod that never became reachable, whose terminate races a transient API
        # timeout then resolves to POD_NOT_FOUND) both lingers in the anti-orphan ledger AND
        # makes provision_reachable's "could not confirm termination" guard halt the whole
        # run on a pod that provably does not exist (2026-07-09). The fail-closed guard is
        # preserved for GENUINELY uncertain cases (a real timeout that never resolves).
        if "POD_NOT_FOUND" in msg or "not found to terminate" in msg:
            _untrack(pod_id)
            print(f"[runpod] terminate({pod_id}): already gone (POD_NOT_FOUND) — untracked",
                  file=sys.stderr)
            return True
        print(
            f"[runpod] terminate({pod_id}) error (verify in console): {redact_secret(e)}",
            file=sys.stderr,
        )
        return False
    _untrack(pod_id)
    return True


def arm_remote_watchdog(pod, ttl_seconds):
    """Schedule a SELF-TERMINATE on the pod itself, independent of this local process.

    Our teardown safety (register_cleanup + finally blocks) only fires if THIS process
    is still alive to run it. If the local session/container is recycled or killed (e.g.
    across a long gap, a crash, or hitting a usage limit mid-run), the local finally
    block never runs and the pod bills forever with nothing to stop it — this happened
    for real on 2026-07-07 (an orphaned pod cost ~$1 before being caught manually).

    This arms a background job ON THE POD that calls RunPod's OWN terminate mutation
    against itself after ttl_seconds, using the same API key already in use for this
    session — a hard backstop that survives the local driver dying, being SIGKILLed, or
    the whole sandbox disappearing. Best-effort: logs a warning but does not raise if the
    SSH call itself fails (the caller's own local teardown is still the primary path)."""
    pod_id = pod["id"]
    payload = json.dumps({"query": f'mutation{{ podTerminate(input:{{podId:"{pod_id}"}}) }}'})
    # Do not put the API key in `bash -c` or `curl` arguments: those are visible
    # to every process that can inspect the pod's process list.  curl can read a
    # header from a root-only file instead.
    auth_path = "/root/.cx_runpod_watchdog_auth"
    local_auth = None
    try:
        with tempfile.NamedTemporaryFile("w", prefix="cx-watchdog-auth-", delete=False) as handle:
            local_auth = handle.name
            handle.write("Authorization: Bearer " + _api_key() + "\n")
        os.chmod(local_auth, 0o600)
        rc, _out, err = ssh(pod, "rm -f " + auth_path, timeout=20)
        if rc != 0:
            print(f"[runpod] remote watchdog auth cleanup failed on {pod_id}: {redact_secret(err)[:200]}", file=sys.stderr)
            return
        ok, err = scp_to(pod, local_auth, auth_path, timeout=30)
        if not ok:
            print(f"[runpod] remote watchdog auth transfer failed on {pod_id}: {redact_secret(err)[:200]}", file=sys.stderr)
            return
        rc, _out, err = ssh(pod, "chmod 600 " + auth_path, timeout=20)
        if rc != 0:
            print(f"[runpod] remote watchdog auth permission failed on {pod_id}: {redact_secret(err)[:200]}", file=sys.stderr)
            return
    finally:
        if local_auth:
            try:
                os.unlink(local_auth)
            except OSError:
                pass
    cmd = (
        f"nohup bash -c 'sleep {int(ttl_seconds)} && "
        f"curl -s -X POST {API} -H \"Content-Type: application/json\" "
        f"-H @{auth_path} -d {json.dumps(payload)}' "
        f"> /root/.watchdog.log 2>&1 & disown"
    )
    try:
        rc, out, err = ssh(pod, cmd, timeout=20)
        if rc == 0:
            print(f"[runpod] remote watchdog armed on {pod_id}: self-terminates in "
                  f"{ttl_seconds}s ({ttl_seconds/60:.0f}m) if nothing else tears it down",
                  file=sys.stderr)
        else:
            print(f"[runpod] remote watchdog arm FAILED (rc={rc}) on {pod_id}: {redact_secret(err)[:200]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[runpod] remote watchdog arm error on {pod_id}: {redact_secret(e)}", file=sys.stderr)


def terminate_all_tracked():
    """The watchdog / cleanup call: nuke every pod this run created."""
    for pod_id in list(_load_tracked()):
        print(f"[runpod] cleanup: terminating {pod_id}", file=sys.stderr)
        terminate(pod_id)


def register_cleanup():
    """Wire teardown to every exit path (normal, exception, SIGINT/SIGTERM)."""
    atexit.register(terminate_all_tracked)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (terminate_all_tracked(), sys.exit(130)))


def balance():
    return gql("query { myself { clientBalance currentSpendPerHr } }")["myself"]


def live_pods():
    return gql(
        "query { myself { pods { id name desiredStatus runtime { uptimeInSeconds } } } }"
    )["myself"]["pods"]


def _pubkey():
    with open(PUBKEY_PATH) as f:
        return f.read().strip()


def _deploy(gpu_type, cloud, image, disk_gb, name):
    q = """mutation($in: PodFindAndDeployOnDemandInput!){
             podFindAndDeployOnDemand(input:$in){ id } }"""
    vin = {
        "cloudType": cloud, "gpuCount": 1, "gpuTypeId": gpu_type, "name": name,
        "imageName": image, "containerDiskInGb": disk_gb, "volumeInGb": 0,
        "ports": "22/tcp,8000/http",
        "env": [{"key": "PUBLIC_KEY", "value": _pubkey()}],
    }
    try:
        d = gql(q, {"in": vin})
        return (d.get("podFindAndDeployOnDemand") or {}).get("id")
    except Exception as e:
        print(
            f"[runpod] deploy {gpu_type}/{cloud} declined: {redact_secret(e)[:80]}",
            file=sys.stderr,
        )
        return None


def _ssh_endpoint(pod_id):
    q = """query($i:String!){ pod(input:{podId:$i}){ runtime {
             uptimeInSeconds ports { ip publicPort privatePort isIpPublic } } } }"""
    rt = (gql(q, {"i": pod_id}).get("pod") or {}).get("runtime")
    if not rt or not rt.get("ports"):
        return None
    for p in rt["ports"]:
        if p.get("privatePort") == 22 and p.get("isIpPublic") and p.get("ip"):
            return p["ip"], p["publicPort"]
    return None


def _ssh_ok(ip, port):
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, "-p", str(port), f"root@{ip}", "echo OK"],
            capture_output=True, text=True, timeout=20)
        return "OK" in r.stdout
    except Exception:
        return False


def _cuda_ok(ip, port):
    """A pod is only USABLE for GPU rungs if torch actually sees the GPU. Some
    RunPod community hosts hand out a card whose CUDA runtime is broken (torch
    imports but torch.cuda.is_available() is False → vLLM engine-init crashes).
    Reject those so the lab lands a genuinely GPU-capable box."""
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, "-p", str(port), f"root@{ip}",
             "python3 -c 'import torch;print(\"CUDA_OK\" if torch.cuda.is_available() else \"CUDA_NO\")'"],
            capture_output=True, text=True, timeout=45)
        return "CUDA_OK" in r.stdout
    except Exception:
        return False


def _cuda_driver_version(ip, port):
    """Return CUDA driver API version reported by torch, e.g. 12080 for CUDA 12.8."""
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, "-p", str(port), f"root@{ip}",
             "python3 -c 'import torch; f=getattr(torch._C, \"_cuda_getDriverVersion\", None); print(f() if f else 0)'"],
            capture_output=True, text=True, timeout=45)
        for tok in reversed(r.stdout.strip().split()):
            if tok.isdigit():
                return int(tok)
    except Exception:
        pass
    return None


def provision_reachable(gpu_plan, image, disk_gb=40, name="cx-spec-lab",
                        reach_tries=10, reach_wait=14, require_cuda=True,
                        min_cuda_driver_version=None, max_deploys=None):
    """
    Try (gpu_type, cloud) combos from gpu_plan once, in order, capped by max_deploys,
    until one lands a pod that is BOTH SSH-reachable (the datacenter-routing wall) AND — when
    require_cuda — has a working CUDA runtime (torch.cuda.is_available(), the broken-host
    wall). If min_cuda_driver_version is set, reject hosts whose CUDA driver API version is
    lower than that floor before paying setup/install time. The plan is deliberately NOT cycled:
    fallback policy must be monotonic upward
    (upgrade, not downgrade) when the caller orders the plan that way. Every un-reachable /
    stuck / CUDA-broken attempt is terminated before moving on, so there is never silent
    billing. Returns {id, ip, port, gpu, cloud} or raises.
    """
    deploys = 0
    plan = list(gpu_plan)
    if not plan:
        raise RuntimeError("empty GPU plan")
    if max_deploys is not None:
        plan = plan[:max_deploys]
    for gpu_type, cloud in plan:
        deploys += 1
        print(f"[runpod] try {deploys}/{len(plan)}: {gpu_type} ({cloud})…", file=sys.stderr)
        pod_id = _deploy(gpu_type, cloud, image, disk_gb, name)
        if not pod_id:
            continue
        _track(pod_id)  # tracked BEFORE we do anything else
        reached = None
        for _ in range(reach_tries):
            try:
                ep = _ssh_endpoint(pod_id)
            except Exception as e:
                print(
                    f"[runpod] {pod_id} endpoint lookup transient error: {redact_secret(e)[:120]}",
                    file=sys.stderr,
                )
                ep = None
            if ep and _ssh_ok(*ep):
                reached = ep
                break
            time.sleep(reach_wait)
        if not reached:
            print(f"[runpod] {pod_id} never reachable — terminating", file=sys.stderr)
            if not terminate(pod_id):
                raise RuntimeError(
                    f"could not confirm termination of unreachable pod {pod_id}; "
                    "stop provisioning and run cleanup"
                )
            continue
        ip, port = reached
        if require_cuda and not _cuda_ok(ip, port):
            print(f"[runpod] {pod_id} reachable but CUDA broken — terminating", file=sys.stderr)
            if not terminate(pod_id):
                raise RuntimeError(
                    f"could not confirm termination of CUDA-broken pod {pod_id}; "
                    "stop provisioning and run cleanup"
                )
            continue
        driver_version = None
        if require_cuda or min_cuda_driver_version is not None:
            driver_version = _cuda_driver_version(ip, port)
        if min_cuda_driver_version is not None:
            if driver_version is None or driver_version < int(min_cuda_driver_version):
                print(
                    f"[runpod] {pod_id} driver {driver_version} < required "
                    f"{min_cuda_driver_version} — terminating",
                    file=sys.stderr,
                )
                if not terminate(pod_id):
                    raise RuntimeError(
                        f"could not confirm termination of driver-incompatible pod {pod_id}; "
                        "stop provisioning and run cleanup"
                    )
                continue
        print(
            f"[runpod] READY: {gpu_type} {pod_id} root@{ip}:{port} "
            f"(cuda={require_cuda}, driver={driver_version})",
            file=sys.stderr,
        )
        return {
            "id": pod_id,
            "ip": ip,
            "port": port,
            "gpu": gpu_type,
            "cloud": cloud,
            "cuda_driver_version": driver_version,
        }
    raise RuntimeError(f"no reachable+usable GPU after {deploys} deploys (network/capacity/CUDA)")


# ---- remote exec ---------------------------------------------------------------

def ssh(pod, cmd, timeout=1200):
    r = subprocess.run(
        ["ssh", *SSH_OPTS, "-p", str(pod["port"]), f"root@{pod['ip']}", cmd],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def scp_to(pod, local, remote, timeout=300):
    r = subprocess.run(
        ["scp", *SSH_OPTS, "-P", str(pod["port"]), "-r", local, f"root@{pod['ip']}:{remote}"],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, r.stderr


def scp_from(pod, remote, local, timeout=1200):
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    r = subprocess.run(
        ["scp", *SSH_OPTS, "-P", str(pod["port"]), "-r", f"root@{pod['ip']}:{remote}", local],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, r.stderr


# ---- detached remote exec (survives transport resets on long commands) ----------
#
# 2026-07-09 incident: a 49-minute 4K/4096-spp render ran as ONE synchronous ssh()
# call and the transport was RESET by the peer ("Connection reset by peer", rc=255)
# after 3 of 4 reference frames had already completed on the GPU. The render was
# healthy; only the SSH connection died, and the whole run's output was lost.
# Keepalives (ServerAliveInterval/CountMax above) cannot survive a peer reset, so a
# long-running remote command must never depend on any single connection.
#
# ssh_detached() launches the command DETACHED on the pod (setsid + nohup; stdout,
# stderr, shell PID and exit code all persisted under /root/.cx_detached/) via one
# SHORT ssh call, then POLLS for the .rc file over short per-poll connections. Any
# individual connection drop — including a peer reset — is tolerated and retried
# until the hard timeout_s deadline. On completion the FULL remote stdout is
# fetched, so callers that parse a final JSON line keep working unchanged.
#
# Money-safety is UNCHANGED: the pod-side watchdog (arm_remote_watchdog) and
# register_cleanup() still bound the pod's lifetime; a detached process simply dies
# with the pod. Callers must keep timeout_s within the driver's existing budget so
# the watchdog ordering stays sane.

DETACHED_DIR = "/root/.cx_detached"
DETACHED_TIMEOUT_RC = 124  # bash/coreutils `timeout` convention


def _sq(text):
    """Single-quote `text` for safe embedding in a POSIX shell command line."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def ssh_detached(pod, cmd, *, workdir=None, tag=None, timeout_s=3600, poll_every=20,
                 launch_timeout=90, poll_timeout=60, tail_lines=3):
    """Run `cmd` on the pod detached from any single SSH connection; poll to completion.

    Returns (rc, stdout, stderr_tail) — the SAME shape as ssh() — where stdout is the
    COMPLETE remote stdout of `cmd` (final-JSON-line contracts keep working) and
    stderr_tail is the last ~4KB of remote stderr.

      * workdir:    optional remote directory to `cd` into before running cmd.
      * tag:        names the artifact files <tag>.{out,err,pid,rc} under DETACHED_DIR
                    (auto-generated if None; reusing a tag overwrites the old artifacts).
      * timeout_s:  hard overall deadline. On expiry the remote process GROUP is killed
                    via the pid file and (DETACHED_TIMEOUT_RC, partial_stdout, message)
                    is returned. Keep this <= the caller's existing timeout budget.
      * poll_every: seconds between completion polls (each poll is its own short ssh
                    connection, so any single drop is harmless).

    Failure tolerance: an individual poll/fetch ssh failure (nonzero rc, local timeout,
    peer reset) NEVER aborts the wait — it is logged and retried until the deadline.
    That is the entire point of this helper.
    """
    if tag is None:
        tag = "job-%d-%d" % (int(time.time()), os.getpid())
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", str(tag)) or "job"
    base = DETACHED_DIR + "/" + tag
    out_f, err_f, pid_f, rc_f = base + ".out", base + ".err", base + ".pid", base + ".rc"
    label = "[runpod] detached[%s]" % tag

    # The detached script: run cmd in a SUBSHELL (so an `exit` inside cmd cannot skip
    # the trailer), then write the exit code ATOMICALLY (tmp + mv) so a poll can never
    # read a half-written rc file.
    script = ("cd " + _sq(workdir) + " && " if workdir else "") + "( " + cmd + " )"
    script += "; rc=$?; echo $rc > " + rc_f + ".tmp && mv " + rc_f + ".tmp " + rc_f

    prep_cmd = ("mkdir -p " + DETACHED_DIR + " && rm -f " + rc_f + " " + pid_f +
                " && : > " + out_f + " && : > " + err_f)
    # setsid makes the detached shell a session+group leader (pgid == pid) so the
    # deadline path can kill the WHOLE render process tree via `kill -- -pid`. If
    # setsid is unavailable (e.g. the local test shim on macOS), plain nohup still
    # detaches the shell from the launching connection. The pid-file guard makes a
    # retried launch a no-op when a previous launch attempt actually took (the
    # ambiguous case: the transport died AFTER the remote shell started the job).
    launch_cmd = (
        "if [ -e " + pid_f + " ]; then echo CX_DETACHED_ALREADY; else "
        "setsid_bin=$(command -v setsid || true); "
        "nohup $setsid_bin bash -c " + _sq(script) +
        " > " + out_f + " 2> " + err_f + " < /dev/null & "
        "echo $! > " + pid_f + "; echo CX_DETACHED_LAUNCHED; fi"
    )
    poll_cmd = (
        "if [ -f " + rc_f + " ]; then echo CX_DETACHED_RC=$(cat " + rc_f + "); fi; "
        "tail -n " + str(int(tail_lines)) + " " + err_f +
        " 2>/dev/null | sed 's/^/CX_TAIL_ERR: /'; "
        "tail -n " + str(int(tail_lines)) + " " + out_f +
        " 2>/dev/null | sed 's/^/CX_TAIL_OUT: /'"
    )

    start = time.monotonic()
    deadline = start + float(timeout_s)

    # -- prep (short ssh; retried — nothing is running yet, so retry is always safe)
    prep_err = "prep never attempted"
    for attempt in range(3):
        try:
            rc0, _out0, err0 = ssh(pod, prep_cmd, timeout=launch_timeout)
        except Exception as e:  # noqa: BLE001 — transport errors are the expected case
            prep_err = "%s: %s" % (type(e).__name__, e)
            print("%s prep attempt %d/3 transport error: %s" % (label, attempt + 1, prep_err),
                  file=sys.stderr)
            continue
        if rc0 == 0:
            prep_err = None
            break
        prep_err = (err0 or "").strip()[-200:]
        print("%s prep attempt %d/3 rc=%s: %s" % (label, attempt + 1, rc0, prep_err),
              file=sys.stderr)
    if prep_err is not None:
        return 255, "", "ssh_detached[%s] prep failed: %s" % (tag, prep_err)

    # -- launch (short ssh; the pid-file guard makes retries double-launch-safe)
    launched = False
    launch_err = "launch never attempted"
    for attempt in range(3):
        try:
            rc1, out1, err1 = ssh(pod, launch_cmd, timeout=launch_timeout)
        except Exception as e:  # noqa: BLE001
            launch_err = "%s: %s" % (type(e).__name__, e)
            print("%s launch attempt %d/3 transport error: %s" % (label, attempt + 1, launch_err),
                  file=sys.stderr)
            rc1, out1 = None, ""
        if rc1 == 0 and ("CX_DETACHED_LAUNCHED" in out1 or "CX_DETACHED_ALREADY" in out1):
            launched = True
            break
        if rc1 is not None:
            launch_err = (err1 or "").strip()[-200:] or ("rc=%s" % rc1)
        # Ambiguous outcome — check whether the job actually took before retrying.
        try:
            rc2, out2, _err2 = ssh(
                pod, "test -e " + pid_f + " && echo CX_PID_PRESENT || echo CX_PID_ABSENT",
                timeout=poll_timeout)
            if rc2 == 0 and "CX_PID_PRESENT" in out2:
                launched = True
                break
        except Exception:  # noqa: BLE001
            pass
    if not launched:
        return 255, "", "ssh_detached[%s] launch failed: %s" % (tag, launch_err)
    print("%s launched (remote logs: %s)" % (label, out_f), file=sys.stderr)

    # -- poll: every connection here is DISPOSABLE; any single failure is tolerated.
    final_rc = None
    last_tail = ""
    while time.monotonic() < deadline:
        time.sleep(max(0.0, min(float(poll_every), deadline - time.monotonic())))
        try:
            rcp, outp, errp = ssh(pod, poll_cmd, timeout=poll_timeout)
        except Exception as e:  # noqa: BLE001 — resets/timeouts here are the whole point
            print("%s poll transport error (tolerated, will retry): %s: %s"
                  % (label, type(e).__name__, e), file=sys.stderr)
            continue
        if rcp != 0:
            print("%s poll rc=%s (tolerated, will retry): %s"
                  % (label, rcp, (errp or "").strip()[-160:]), file=sys.stderr)
            continue
        tail = "\n".join(l for l in (outp or "").splitlines() if l.startswith("CX_TAIL_"))
        if tail and tail != last_tail:
            last_tail = tail
            print("%s progress @%ds:\n%s" % (label, int(time.monotonic() - start), tail),
                  file=sys.stderr)
        match = re.search(r"^CX_DETACHED_RC=(-?\d+)\s*$", outp or "", re.M)
        if match:
            final_rc = int(match.group(1))
            break

    if final_rc is None:
        # One last look — the job may have finished between the final poll and the deadline.
        try:
            rcl, outl, _errl = ssh(pod, "cat " + rc_f + " 2>/dev/null || true",
                                   timeout=poll_timeout)
            m = re.search(r"-?\d+", outl or "")
            if rcl == 0 and m:
                final_rc = int(m.group(0))
        except Exception:  # noqa: BLE001
            pass

    if final_rc is None:
        # Hard deadline: kill the remote process group via the pid file (best effort;
        # the pod-side watchdog remains the money-safety backstop either way).
        kill_cmd = (
            "if [ -f " + pid_f + " ]; then pgid=$(cat " + pid_f + "); "
            "kill -TERM -- -$pgid 2>/dev/null || kill -TERM $pgid 2>/dev/null; sleep 1; "
            "kill -KILL -- -$pgid 2>/dev/null || kill -KILL $pgid 2>/dev/null; fi; true"
        )
        try:
            ssh(pod, kill_cmd, timeout=45)
        except Exception as e:  # noqa: BLE001
            print("%s deadline kill best-effort failed: %s: %s"
                  % (label, type(e).__name__, e), file=sys.stderr)
        best_out = ""
        try:
            rco, outo, _erro = ssh(pod, "cat " + out_f + " 2>/dev/null || true",
                                   timeout=poll_timeout)
            if rco == 0:
                best_out = outo or ""
        except Exception:  # noqa: BLE001
            pass
        return (DETACHED_TIMEOUT_RC, best_out,
                "ssh_detached[%s] deadline: no completion after %ss; "
                "remote process group killed via %s" % (tag, timeout_s, pid_f))

    # -- completed: fetch the FULL stdout (the caller's final-JSON-line contract) and a
    #    stderr tail. Fetches are retried too — a drop here must not lose a finished run.
    out_full = None
    fetch_err = "stdout fetch never attempted"
    for attempt in range(5):
        try:
            rcf, outf, errf = ssh(pod, "cat " + out_f, timeout=max(int(poll_timeout), 120))
        except Exception as e:  # noqa: BLE001
            fetch_err = "%s: %s" % (type(e).__name__, e)
            print("%s stdout fetch attempt %d/5 transport error (will retry): %s"
                  % (label, attempt + 1, fetch_err), file=sys.stderr)
            time.sleep(min(float(poll_every), 10.0))
            continue
        if rcf == 0:
            out_full = outf if outf is not None else ""
            break
        fetch_err = (errf or "").strip()[-200:] or ("rc=%s" % rcf)
        print("%s stdout fetch attempt %d/5 rc=%s (will retry): %s"
              % (label, attempt + 1, rcf, fetch_err), file=sys.stderr)
        time.sleep(min(float(poll_every), 10.0))
    if out_full is None:
        return 255, "", ("ssh_detached[%s] completed rc=%d but stdout fetch failed: %s"
                         % (tag, final_rc, fetch_err))

    err_tail = ""
    for _attempt in range(3):
        try:
            rce, oute, _erre = ssh(pod, "tail -c 4000 " + err_f + " 2>/dev/null || true",
                                   timeout=poll_timeout)
        except Exception:  # noqa: BLE001
            continue
        if rce == 0:
            err_tail = oute or ""
            break
    print("%s done rc=%d in %ds" % (label, final_rc, int(time.monotonic() - start)),
          file=sys.stderr)
    return final_rc, out_full, err_tail


if __name__ == "__main__":
    # `python3 runpod.py cleanup` — the manual money-safety escape hatch.
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        terminate_all_tracked()
        remaining = _load_tracked()
        if remaining:
            print(f"cleanup attempted; still tracked: {remaining}", file=sys.stderr)
            sys.exit(1)
        print("cleanup done; tracked pods cleared.")
    elif len(sys.argv) > 1 and sys.argv[1] == "balance":
        print(json.dumps(balance()))
    elif len(sys.argv) > 1 and sys.argv[1] == "pods":
        print(json.dumps(live_pods()))
    else:
        print("usage: runpod.py [cleanup|balance|pods]")
