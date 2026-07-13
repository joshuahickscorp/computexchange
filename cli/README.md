# cx — Computexchange buyer CLI

A single stdlib-only Go binary for the buyer REST API. For a development build,
run `cd cli && go build -o cx .`. `cx version --json` reports whether a binary is
a development build or a release with injected version, commit, build time, Go
toolchain, and target platform.

```bash
export CX_API_URL=http://localhost:8080   # control plane (this is the default)
export CX_API_KEY=<your buyer api key>    # sent as Authorization: Bearer
printf '{"text":"hello"}\n{"text":"world"}\n' | cx submit --model all-minilm-l6-v2 --type embed --wait
cx status <job_id> ; cx results <job_id> ; cx models ; cx estimate --model all-minilm-l6-v2 --units 50000
```

Reads JSONL from `--input <file>` or stdin (`-`), POSTs the job, prints `job_id`; `--wait` polls to completion and streams the merged result. Any non-2xx response prints the status line + body and exits non-zero. Run `cx submit -h` for the full flag list (`--type`, `--labels`, `--max-tokens`, `--top-k`, `--schema`, `--tier`, `--redundancy`, `--split`, ...).

## Release artifacts

From the repository root, build checksummed macOS and Linux archives plus a
machine-readable manifest:

```bash
scripts/build-cli-release.sh v0.1.0
```

Artifacts land under `.artifacts/releases/cli/v0.1.0/`. This does not publish or
tag anything. `scripts/verify-cli-release.sh` builds a snapshot, extracts the
native archive into a clean temporary directory, verifies its checksum and
release identity, and runs the help smoke test.
