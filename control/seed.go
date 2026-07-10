package main

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// seed.go — `control seed`: idempotently create the minimum rows a human needs to
// drive the whole system end to end, and print the credentials.
//
// Everything uses stable demo UUIDs + fixed tokens and ON CONFLICT DO NOTHING, so
// re-running is safe (no duplicates, no clobbering operator edits). api_keys
// stores only the SHA-256 hash of a key (hashKey), so the raw keys printed here
// are the only place they appear — copy them when you seed. The base schema
// already seeds the models catalogue; we ensure it is present anyway so a fresh
// DB that only ran Migrate (not schema.sql) still has a priced catalogue.

// Fixed demo identities + credentials. These are DEV placeholders, printed in
// the clear on purpose; rotate them before any non-local use.
const (
	demoSupplierID   = "00000000-0000-0000-0000-0000000000a1"
	demoSupplierID2  = "00000000-0000-0000-0000-0000000000a2"
	demoWorkerID     = "00000000-0000-0000-0000-0000000000b1"
	demoWorkerID2    = "00000000-0000-0000-0000-0000000000b2"
	demoBuyerID      = "00000000-0000-0000-0000-0000000000c1"
	demoAdminBuyerID = "00000000-0000-0000-0000-0000000000c2"

	demoWorkerToken  = "dev-worker-token-0001"
	demoWorkerToken2 = "dev-worker-token-0002"
	demoAPIKey       = "dev-api-key-0001"
	demoAdminAPIKey  = "dev-admin-key-0001"

	demoHoneypotEmbedRef  = "honeypots/embed/0001/input.jsonl"
	demoHoneypotInferRef  = "honeypots/batch_infer/0001/input.jsonl"
	demoHoneypotEmbedText = "the quick brown fox jumps over the lazy dog"

	// The verification class that produced demoHoneypotHawkKnownAnswer (see its
	// doc comment): the reference M3 Pro registering as a `hawking` worker.
	// build_hash is the box's REAL registration-path identity
	// (hardware.rs engine_build_hash("hawking", agent_version) — agent 0.1.0,
	// device_label=metal, q4_k_m, infer_content_id at capture time), printed by
	// the harness itself; NEVER hand-compute or hand-edit it. On any OTHER
	// box/build the class never matches, so the probe safely skips (a coverage
	// gap, never a wrongful quarantine) until the operator re-generates.
	demoHoneypotHawkEngine    = "hawking"
	demoHoneypotHawkBuildHash = "a0ce01606255c06e"

	// The byte-exact hawking honeypot's VALIDITY BOUNDS, now enforced at injection
	// time (the guard the demoHoneypotHawkKnownAnswer doc names as REQUIRED before
	// production-scale byte-exact seeding). demoHoneypotHawkKnownAnswer is byte-valid
	// ONLY for a batch_infer job on demoHoneypotHawkModel with
	// max_tokens >= demoHoneypotHawkMinMaxTokens: the answer was captured on
	// llama-3.2-1b-instruct-q4 (its own "model" field), and every row EOS'd strictly
	// below max_tokens=24 (max row: 10 tokens) so the bytes are invariant only for a
	// dispatching job whose max_tokens is at least 24. Persisted into
	// honeypots.answer_model / answer_min_max_tokens so AvailableSeedHoneypots refuses
	// to draw this probe for a job it is not byte-valid for — a batch_infer job on a
	// DIFFERENT model, or with max_tokens < 24, where an HONEST same-class worker
	// legitimately produces different bytes and would otherwise be wrongly quarantined.
	demoHoneypotHawkModel        = "llama-3.2-1b-instruct-q4"
	demoHoneypotHawkMinMaxTokens = 24
)

// demoHoneypotHawkInputJSONL is the hawking honeypot's input chunk — the exact
// JSONL bytes the seed uploads at demoHoneypotInferRef and the agent-side
// harness (runners.rs
// hawking_honeypot_seed_blob_membership_stable_across_pool_sizes) recorded the
// known answer for. Six short factual prompts chosen for a WELL-SEPARATED
// greedy path; the harness REJECTED the first candidate set's sixth prompt
// ("The opposite of hot is") because its argmax genuinely near-ties and flips
// with co-batch membership (pool_size=1 vs 2) — see the membership-stability
// requirement in docs/DETERMINISM_CLASS.md before ever editing this chunk.
const demoHoneypotHawkInputJSONL = `{"id":"0","prompt":"The capital of France is"}
{"id":"1","prompt":"The largest planet in our solar system is"}
{"id":"2","prompt":"What color is the sky on a clear day? Answer in one word."}
{"id":"3","prompt":"Two plus two equals"}
{"id":"4","prompt":"The chemical symbol for water is"}
{"id":"5","prompt":"The chemical symbol for gold is"}
`

// demoHoneypotHawkKnownAnswer is the REAL BatchInferResult document the REAL
// hawking engine (HawkingRunner::run — the exact production dispatch path, real
// Llama-3.2-1B-Instruct Q4_K_M GGUF on real Metal) produced for
// demoHoneypotHawkInputJSONL on the reference M3 Pro, 2026-07-06 — not a
// hand-typed placeholder. Captured by the `#[ignore]`d real-Metal harness
// `runners::tests::hawking_honeypot_seed_blob_membership_stable_across_pool_sizes`,
// which is the ONLY sanctioned way to produce this value because it also PROVES
// the two properties that make a byte-exact hawking honeypot safe to seed:
//
//  1. CO-BATCH MEMBERSHIP STABILITY — hawking_pool_size is operator-configurable
//     (1..=8), so the same chunk decodes under different slot memberships on
//     different workers of the SAME class, and a genuine argmax near-tie can
//     flip a token with membership (the characterized reduction-order property;
//     11/24 free-form rows diverged in the 2026-07-06 dispatch measurement).
//     The harness asserts this document is BYTE-IDENTICAL at pool_size 1, 2, 4
//     and 8; an unstable answer would auto-quarantine an honest same-class
//     worker that merely runs a different pool size.
//  2. NATURAL EOS BELOW max_tokens — every row terminates at EOS strictly below
//     the recorded max_tokens=24 (max row: 10), so the bytes are invariant to
//     any dispatching job's max_tokens >= 24. Honeypots ride on real buyer jobs
//     and inherit their params (api.go), so a truncated row's bytes would
//     depend on the buyer's max_tokens.
//
// KNOWN VALIDITY BOUNDS (now ENFORCED at injection time): the answer is only
// valid evidence for batch_infer jobs on llama-3.2-1b-instruct-q4 with
// max_tokens >= 24. These bounds are persisted into honeypots.answer_model /
// answer_min_max_tokens (the seed INSERT below writes demoHoneypotHawkModel /
// demoHoneypotHawkMinMaxTokens), and AvailableSeedHoneypots now filters on the
// job's model + max_tokens, so a batch_infer job on a DIFFERENT model (or with a
// smaller max_tokens) NO LONGER draws this probe — closing the gap where an
// HONEST same-class worker running such a job would byte-fail and be wrongly
// quarantined. The guard docs/speed-lane-reports/HAWKING_REGATE_WAVE2B.md named
// as REQUIRED before production-scale byte-exact seeding is thus landed.
const demoHoneypotHawkKnownAnswer = `{"job_type":"batch_infer","model":"llama-3.2-1b-instruct-q4","completions":[{"index":0,"text":"The capital of France is Paris.","tokens":7},{"index":1,"text":"The largest planet in our solar system is Jupiter.","tokens":10},{"index":2,"text":"Blue","tokens":1},{"index":3,"text":"The answer is 4.","tokens":6},{"index":4,"text":"The chemical symbol for water is H2O.","tokens":10},{"index":5,"text":"The chemical symbol for gold is Au.","tokens":8}]}`

// demoHoneypotEmbedKnownAnswer is the REAL all-minilm-l6-v2 embedding of
// demoHoneypotEmbedText — {"id":"honeypot-probe","text":"...same string..."} run
// through the actual live Rust/Candle/Metal agent (not a hand-typed placeholder).
//
// HARDENING FIX (Buyer Developer Experience 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md):
// running the real `openai` SDK against a from-scratch seeded stack surfaced two real
// bugs the mocked/synthetic test harness never exercised, because every existing test
// (mustJobTask, driveOneTask) fabricates the honeypot's dispatch/result in-process and
// never has a real worker actually GET the honeypot's presigned input_url from object
// storage:
//  1. The honeypot DB row (below) named an input_ref that was NEVER uploaded as an
//     object — any real worker fetching it got a real 404 and retried forever, so an
//     OpenAI-compatible batch job with the (now-mandatory, see api.go
//     wantVerificationFloor) honeypot floor could never complete against a fresh seed.
//  2. The known_answer used to be the placeholder `{"vectors":[[1,0,0]]}` — a 3-dim
//     vector that can never cosine-match a real 384-dim MiniLM embedding
//     (meanCosine's length check alone forces sim=0), so ANY honest worker that
//     actually computed the honeypot would be wrongly flagged as a mismatch and
//     hard-quarantined (DockReputation + ClawbackTaskCredit + QuarantineSupplier) for
//     giving the CORRECT answer. Embed honeypots are always byte/cosine-comparable
//     (byteHoneypotComparable's tolerant-job-type branch), so this was live, not
//     latent: the very first real honeypot check against a real worker would have
//     fired it.
//
// This constant is the fix for (2): a real vector, measured once against the actual
// shipped model, so cosine agreement is genuine instead of guaranteed-to-fail.
const demoHoneypotEmbedKnownAnswer = `{"vectors":[[0.035496805,0.061286226,0.05269204,0.07070498,0.033101425,-0.030669669,0.006620546,-0.0611833,-0.0013259869,0.010645743,0.038649973,0.039953217,-0.03836758,-0.016668865,-0.005615571,-0.02435592,-0.035996914,-0.030242963,0.058470055,-0.04949615,-0.0772954,-0.05238774,0.024527121,0.029310636,-0.07390914,-0.024959233,-0.06531419,-0.042886484,0.07116563,-0.11381945,-0.012659401,0.039626047,-0.021003585,0.017806444,-0.031887453,-0.09112297,0.059122432,-0.0073039983,0.033136763,0.02990603,0.04216888,-0.016912952,-0.045001578,0.029674461,-0.09925842,0.053289246,-0.076478474,-0.014867955,0.015249468,0.013789408,-0.044192377,-0.027839303,0.0067307525,0.056497026,0.07217815,-0.004120588,-0.003776597,-0.03550878,0.049068395,-0.010343076,0.023608431,0.036382392,0.018006727,-0.00094273675,0.038770657,0.02314508,-0.027165852,-0.08001895,-0.097672306,0.0039906693,0.013621336,-0.04742567,-0.016779883,-0.00950412,0.0048912084,-0.028031033,0.055237602,-0.05924872,0.061445322,0.003547026,-0.029831512,-0.054971755,-0.05296839,0.04703358,0.034341488,0.0055239666,0.028062375,0.03031389,-0.014329243,-0.035245396,-0.028658535,-0.062313855,-0.042015076,0.024477785,0.005535661,0.008140486,0.015373298,-0.04852281,-0.06482945,0.024688132,0.014986816,0.018006658,0.12357855,0.021402664,-0.016756006,-0.04693977,0.0059994524,0.008195311,0.0956789,0.025820956,-0.012012531,-0.0057257675,-0.008574071,0.10505295,0.027633177,0.008675295,-0.06765752,-0.026771478,-0.04068135,-0.10379482,0.076628745,0.12635586,-0.08593975,0.012013857,-0.02571091,-0.050986372,-0.03283179,-2.0257878e-33,0.07332519,-0.024086602,-0.08005569,-0.06789681,-0.051604167,-0.07831673,-0.013334951,-0.02678103,-0.025032565,0.046942182,-0.073741406,-0.0002621599,0.013088732,-0.030957244,-0.02001504,-0.11604217,0.002146202,-0.012764415,0.029652465,0.055050556,0.03083231,0.10599332,-0.03803288,-0.027411968,0.052459177,-0.020513028,-0.07187954,-0.033774283,-0.01512769,0.04966495,-0.04126425,-0.04230653,-0.04000168,0.0903003,-0.023720859,-0.13058026,0.06223134,-0.057030544,-0.03234598,0.06054996,-0.0060443296,0.014045863,0.032446183,0.026641786,-0.06910866,-0.0010001507,0.028158747,0.014681091,-0.00016439879,0.025803452,-0.026630212,0.015755974,0.05387261,-0.053353127,-0.055436786,0.090057075,0.07702737,-0.024442434,-0.034736823,0.09827988,0.030449815,-0.020008767,0.0045398497,-0.04741984,0.14264078,-0.06860561,-0.08137576,0.0010530679,-0.017835073,0.072983496,0.016481595,0.039840028,0.046836257,-0.14453495,0.040242486,-0.03141819,0.015354579,-0.033685256,0.038316358,-0.029271988,0.120120674,-0.08052784,-0.047789346,0.04573376,-0.020778392,0.061083343,0.007391645,0.019994978,-0.01496153,-0.03895476,-0.04937919,-0.0080710305,0.049125556,-0.049061596,0.068764016,1.0519303e-33,0.096452974,-0.04499978,0.070859194,0.07015503,-0.030735904,0.05322887,-0.0071348534,0.045257784,-0.07715489,0.06130448,-0.025757264,0.008330902,-0.0016587618,-0.0004160963,0.11371326,-0.00025955972,0.06547718,-0.006393685,0.027958669,0.015104655,-0.046889227,0.039599907,-0.018600864,0.06945135,0.032981567,0.05686179,0.087666236,-0.025319781,-0.043683805,-0.10387728,-0.0524877,-0.057149,-0.011106056,-0.046786018,0.01876317,0.047879476,-0.04179452,-0.0065929783,-0.021846421,-0.08242988,0.030867599,-0.0012409291,0.023495203,0.07132263,0.027287915,0.0030886792,-0.0566032,0.049843464,-0.037691873,0.062974595,-0.0034526705,0.0384236,0.039379764,0.027615566,-0.049677085,-0.054054372,0.0046572313,-0.04017414,0.03905366,-0.011056407,0.008109523,0.024777273,-0.012472585,-0.003208307,-0.0067499857,-0.08953937,-0.07463353,-0.053929847,0.0771142,-0.0748032,-0.005912122,0.030030748,0.009539191,-0.070892684,0.009316389,0.07843443,0.11027205,0.0049347305,0.0726145,-0.039179403,0.011564601,-0.01696438,-0.0015485858,0.011365727,-0.06918987,0.036279775,-0.115796976,0.07050562,0.042879507,-0.06565238,0.025752466,0.09054123,0.058915827,0.08486909,-0.012912725,-1.7610805e-08,-0.051039536,0.013469737,-0.09776186,0.044388857,0.08008573,0.020573603,-0.03201809,0.012061245,0.08373447,-0.030436367,0.03553867,0.025044776,0.058650985,0.04106562,-0.022832928,0.017844671,-0.03640214,0.010211304,0.028805157,0.16128671,-0.004238813,-0.05567724,-0.010912238,-0.027062437,-0.05236588,-0.03657376,-0.084772125,0.0055240453,-0.03134505,0.013055406,-0.05084199,0.096892275,-0.087066025,0.00086432224,0.034363087,0.031639475,0.1018616,-0.0009797525,0.026641646,0.008034145,0.00895352,0.035023842,-0.02047824,-0.0073480434,-0.07614934,-0.006330164,-0.031122243,-0.10251452,0.074952886,-0.05157088,-0.04738342,-0.04236357,0.042794693,0.06561871,-0.049979053,0.0010257981,-0.0054031373,-0.06540732,-0.04585866,0.036134742,0.06257336,0.054683153,0.05382331,0.08676746]]}`

// seedDemo runs the idempotent seed against the pool and prints the credentials.
// storage is OPTIONAL (nil is fine — `control seed` still runs with no object store
// configured, matching this command's documented "no object store required"
// contract) but, when provided, seedDemo uploads the actual honeypot input object so
// a real worker's presigned-URL fetch succeeds instead of 404ing forever. Skipping
// the upload only skips honeypot COVERAGE (the DB row still exists harmlessly with
// no matching object — AvailableHoneypots would offer it, and a real dispatch
// against it would repeat the exact bug this fix addresses) — so any caller that
// intends to run real jobs against the seed should always pass a real Storage.
func seedDemo(ctx context.Context, pool *pgxpool.Pool, storage *Storage) error {
	// The hawking honeypot's class, in the verifier's own classKey format —
	// assembled through classKey (never a hand-formatted string) and validated
	// through the same guard Store.InsertHoneypot enforces, so the seed can
	// never write the dead/dangerous class-blind byte-exact row item 11 forbids.
	hawkClass := classKey(demoHoneypotHawkEngine, demoHoneypotHawkBuildHash)
	if err := validateHoneypotSeed("batch_infer", hawkClass); err != nil {
		return fmt.Errorf("seed: hawking honeypot: %w", err)
	}
	stmts := []struct {
		sql  string
		args []any
	}{
		// Active supplier (so its workers are eligible) with a healthy reputation.
		{`INSERT INTO suppliers (id, email, reputation, tier, status)
		  VALUES ($1, 'demo-supplier@example.com', 0.90, 2, 'active')
		  ON CONFLICT (id) DO NOTHING`, []any{demoSupplierID}},
		// A worker for that supplier, seen now so it passes liveness. It advertises
		// the demo job types + models so the Scheduler V2 hard filter (ClaimTask)
		// lets it claim the demo embed/infer jobs; min_payout_usd_hr 0 = takes any
		// rate; thermal_ok true. supplier data_country is set so any data-residency
		// constraint can match.
		{`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                       supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		  VALUES ($1, $2, 'apple_silicon_max', 64, 400, now(), 'seed',
		          ARRAY['embed','batch_infer','batch_classification','json_extraction','rerank'],
		          ARRAY['all-minilm-l6-v2','llama-3.2-1b-instruct-q4'], 0, true)
		  ON CONFLICT (id) DO NOTHING`, []any{demoWorkerID, demoSupplierID}},
		// Give the demo supplier a data_country so data-residency-constrained jobs
		// can match it (harmless when a job is unrestricted).
		{`UPDATE suppliers SET data_country = 'US' WHERE id = $1 AND data_country IS NULL`,
			[]any{demoSupplierID}},
		// Worker token bound to that worker/supplier.
		{`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		  VALUES ($1, $2, $3, false)
		  ON CONFLICT (token_hash) DO NOTHING`, []any{hashKey(demoWorkerToken), demoWorkerID, demoSupplierID}},
		// A SECOND supplier + worker + token, so a local multi-supplier run (two
		// agent processes on one box — the local stand-in for "two+ Macs running
		// real jobs end-to-end") has a distinct second identity. Same class +
		// capabilities so within-class redundancy compares the two.
		{`INSERT INTO suppliers (id, email, reputation, tier, status)
		  VALUES ($1, 'demo-supplier-2@example.com', 0.90, 2, 'active')
		  ON CONFLICT (id) DO NOTHING`, []any{demoSupplierID2}},
		{`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                       supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		  VALUES ($1, $2, 'apple_silicon_max', 64, 400, now(), 'seed',
		          ARRAY['embed','batch_infer','batch_classification','json_extraction','rerank'],
		          ARRAY['all-minilm-l6-v2','llama-3.2-1b-instruct-q4'], 0, true)
		  ON CONFLICT (id) DO NOTHING`, []any{demoWorkerID2, demoSupplierID2}},
		{`UPDATE suppliers SET data_country = 'US' WHERE id = $1 AND data_country IS NULL`,
			[]any{demoSupplierID2}},
		{`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		  VALUES ($1, $2, $3, false)
		  ON CONFLICT (token_hash) DO NOTHING`, []any{hashKey(demoWorkerToken2), demoWorkerID2, demoSupplierID2}},
		// Non-admin buyer API key (hash stored, raw printed).
		{`INSERT INTO api_keys (buyer_id, key_hash, is_admin, revoked)
		  VALUES ($1, $2, false, false)
		  ON CONFLICT (key_hash) DO NOTHING`, []any{demoBuyerID, hashKey(demoAPIKey)}},
		// Admin buyer API key.
		{`INSERT INTO api_keys (buyer_id, key_hash, is_admin, revoked)
		  VALUES ($1, $2, true, false)
		  ON CONFLICT (key_hash) DO NOTHING`, []any{demoAdminBuyerID, hashKey(demoAdminAPIKey)}},
		// Ensure the priced model catalogue exists (mirrors schema.sql seed).
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('all-minilm-l6-v2','minilm',NULL,'embed',384,'embed',0.00100000,NULL,2,'sentence-transformers/all-MiniLM-L6-v2')
		  ON CONFLICT (id) DO NOTHING`, nil},
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('bge-small-en-v1.5','bge',NULL,'embed',384,'embed',0.00100000,NULL,2,'BAAI/bge-small-en-v1.5')
		  ON CONFLICT (id) DO NOTHING`, nil},
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('llama-3.2-1b-instruct-q4','llama','q4_k_m','gguf',NULL,'batch_infer',0.00200000,NULL,4,'unsloth/Llama-3.2-1B-Instruct-GGUF')
		  ON CONFLICT (id) DO NOTHING`, nil},
		{`INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo)
		  VALUES ('qwen2.5-7b-instruct-q4','qwen','q4_k_m','gguf',NULL,'batch_infer',0.00800000,NULL,40,'bartowski/Qwen2.5-7B-Instruct-GGUF')
		  ON CONFLICT (id) DO NOTHING`, nil},
		// A couple of honeypots with known answers so honeypot tasks have probes. The
		// known_answer is a REAL measured embedding (demoHoneypotEmbedKnownAnswer's
		// doc comment) — not a placeholder — so an honest worker that actually
		// computes demoHoneypotEmbedText's embedding genuinely cosine-agrees.
		{`INSERT INTO honeypots (job_type, input_ref, known_answer)
		  SELECT 'embed', $1, $2
		  WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type='embed' AND input_ref=$1)`,
			[]any{demoHoneypotEmbedRef, []byte(demoHoneypotEmbedKnownAnswer)}},
		// The batch_infer (byte-exact) honeypot, seeded WITH its producing class
		// (Week 6b, the hawking cross-worker determinism re-gate). This used to be
		// deliberately absent: a byte-exact honeypot MUST carry the answer_class of
		// the worker that produced its known answer (Store.InsertHoneypot /
		// validateHoneypotSeed) — a blank-class placeholder can never fire
		// (byteHoneypotComparable) and a fake answer would wrongly quarantine. The
		// known answer here is a REAL output of the REAL hawking engine on this
		// repo's reference box (demoHoneypotHawkKnownAnswer's doc comment: real
		// GGUF, real Metal, the production HawkingRunner::run path, proven
		// co-batch-membership-stable at pool_size 1/2/4/8), so an honest worker of
		// exactly that (engine, build_hash) class genuinely byte-matches. Workers
		// of every OTHER class (candle, a different build, an unknown build) are
		// never byte-compared against it — the probe skips, which is reduced
		// coverage, never a wrongful quarantine. HONEST COVERAGE NOTE: this row's
		// existence means byte-exact honeypot coverage EXISTS only for the one
		// seeded class; every other class still runs with zero byte-exact honeypot
		// coverage until an operator re-generates the blob on their own reference
		// box (the runners.rs harness in the constant's doc comment) and seeds it
		// operationally.
		// answer_model + answer_min_max_tokens carry the byte-exact validity bounds
		// (llama-3.2-1b-instruct-q4, max_tokens >= 24 — see the demoHoneypotHawk*
		// constants) so the injection-time guard (AvailableSeedHoneypots) only draws
		// this probe for a job it is actually byte-valid for; drawing it for a
		// different model or a smaller max_tokens would wrongly quarantine an HONEST
		// same-class worker whose bytes legitimately differ.
		{`INSERT INTO honeypots (job_type, input_ref, known_answer, answer_class, answer_model, answer_min_max_tokens)
		  SELECT 'batch_infer', $1, $2, $3, $4, $5
		  WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type='batch_infer' AND input_ref=$1)`,
			[]any{demoHoneypotInferRef, []byte(demoHoneypotHawkKnownAnswer), hawkClass, demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens}},
		// BACKFILL (idempotent, fail-OPEN repair): a batch_infer honeypot row seeded
		// BEFORE answer_model/answer_min_max_tokens existed keeps them NULL, which
		// AvailableSeedHoneypots reads as "tolerant" — so the byte-exact hawking probe
		// could be drawn for ANY model / smaller max_tokens and wrongly quarantine an
		// HONEST same-class worker whose bytes legitimately differ (a silent fail-OPEN of
		// the injection guard the INSERT above was added to close). Because that INSERT is
		// WHERE NOT EXISTS, it never heals such a pre-existing row. This UPDATE stamps the
		// canonical bounds on exactly that vulnerable row: matched by the demo input_ref
		// (never an operator's differently-bounded custom honeypot at another ref) and
		// gated on NULL bounds, so it is a no-op once healed and idempotent across reseeds.
		{`UPDATE honeypots
		  SET answer_model=$2, answer_min_max_tokens=$3, answer_class=$4
		  WHERE job_type='batch_infer' AND input_ref=$1
		    AND (answer_model IS NULL OR answer_min_max_tokens IS NULL)`,
			[]any{demoHoneypotInferRef, demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens, hawkClass}},
	}
	for _, st := range stmts {
		if _, err := pool.Exec(ctx, st.sql, st.args...); err != nil {
			return fmt.Errorf("seed %q: %w", st.sql, err)
		}
	}

	// Upload the honeypot's actual INPUT object — the DB row above only records
	// that a honeypot task should point at this key; the object itself must exist
	// in the store or a real worker's presigned GET 404s and the task retries
	// forever (see this function's doc comment, bug (1)). Best-effort: `control
	// seed` is documented to run with no object store configured at all, so a nil
	// storage just skips coverage rather than failing the whole seed.
	if storage != nil {
		embedInput := fmt.Sprintf("{\"id\":\"honeypot-probe\",\"text\":%q}\n", demoHoneypotEmbedText)
		if err := storage.PutObject(ctx, demoHoneypotEmbedRef, []byte(embedInput), "application/x-ndjson"); err != nil {
			return fmt.Errorf("seed: uploading honeypot input object %q: %w", demoHoneypotEmbedRef, err)
		}
		// The hawking honeypot's input chunk: byte-for-byte the JSONL the known
		// answer was recorded for (the harness blob's input_jsonl). Same
		// rationale as the embed object above — the DB row alone would 404 a
		// real worker's presigned GET forever.
		if err := storage.PutObject(ctx, demoHoneypotInferRef, []byte(demoHoneypotHawkInputJSONL), "application/x-ndjson"); err != nil {
			return fmt.Errorf("seed: uploading honeypot input object %q: %w", demoHoneypotInferRef, err)
		}
	}

	// Sanity: confirm the demo UUIDs parse (they are compile-time constants, but
	// a typo would otherwise surface only as a silent FK failure above).
	for _, id := range []string{demoSupplierID, demoSupplierID2, demoWorkerID, demoWorkerID2, demoBuyerID, demoAdminBuyerID} {
		if _, err := uuid.Parse(id); err != nil {
			return fmt.Errorf("seed: bad demo uuid %q: %w", id, err)
		}
	}

	fmt.Println("seed complete — demo credentials (DEV ONLY):")
	fmt.Printf("  supplier_id   = %s\n", demoSupplierID)
	fmt.Printf("  worker_id     = %s\n", demoWorkerID)
	fmt.Printf("  worker_token  = %s   (X-Worker-Token header)\n", demoWorkerToken)
	fmt.Printf("  worker_token2 = %s   (second supplier — local multi-agent run)\n", demoWorkerToken2)
	fmt.Printf("  api_key       = %s   (Authorization: Bearer ...)\n", demoAPIKey)
	fmt.Printf("  admin_api_key = %s   (Authorization: Bearer ..., admin routes)\n", demoAdminAPIKey)
	fmt.Printf("  byte-exact batch_infer honeypot class = %s (fires ONLY for workers of this exact class; all other classes skip — docs/DETERMINISM_CLASS.md)\n", hawkClass)
	return nil
}
