# cx — Computexchange buyer CLI

A single stdlib-only Go binary for the buyer REST API. `cd cli && go build -o cx .`

```bash
export CX_API_URL=http://localhost:8080   # control plane (this is the default)
export CX_API_KEY=<your buyer api key>    # sent as Authorization: Bearer
printf '{"text":"hello"}\n{"text":"world"}\n' | cx submit --model all-minilm-l6-v2 --type embed --wait
cx status <job_id> ; cx results <job_id> ; cx models ; cx estimate --model all-minilm-l6-v2 --units 50000
```

Reads JSONL from `--input <file>` or stdin (`-`), POSTs the job, prints `job_id`; `--wait` polls to completion and streams the merged result. Any non-2xx response prints the status line + body and exits non-zero. Run `cx submit -h` for the full flag list (`--type`, `--labels`, `--max-tokens`, `--top-k`, `--schema`, `--tier`, `--redundancy`, `--split`, ...).
