package main

import "context"

// pricing_extra.go — Store methods added by BUNDLE B+K (quote/pricing engine +
// the honeypot input-GET leak). Kept in a SEPARATE file, not store.go, so this
// work never conflicts with the store.go edits landing concurrently in other
// bundles. Everything here is a plain *Store method or pure helper; no schema
// migration is introduced (see AvailableSeedHoneypots' doc for why the honeypot
// alias split needs none).

// SeedHoneypot is one dispatchable seed honeypot: its input object key plus the
// known answer + verification class needed to (a) copy its input bytes to a
// per-task opaque key and (b) register that opaque key as an alias so the
// verifier's (job_type, input_ref) answer lookup still resolves.
type SeedHoneypot struct {
	InputRef    string // the seeded honeypot object key (always under "honeypots/...")
	KnownAnswer []byte // the measured known answer (may be nil for a class-blind seed)
	AnswerClass string // "engine|build_hash" the answer was produced under ("" = class-blind)
}

// AvailableSeedHoneypots returns up to limit DISPATCHABLE seed honeypots for a
// job — the real, operator/seed-created probes, NEVER a per-task alias this
// bundle writes for the input-GET-leak fix (RegisterHoneypotAlias below).
//
// Why a new method instead of Store.AvailableHoneypots: the honeypot input-GET
// leak fix (Verification & Result Trust 5->5.5, docs/internal/CREED_AND_PATH_TO_TEN.md)
// copies each honeypot's input to an OPAQUE per-task key and registers that key
// as a honeypots-table alias (so GetHoneypotAnswer(job_type, opaque_key) still
// resolves at verify time). Those alias rows must NEVER be re-selected as a
// dispatchable honeypot for a LATER job — their input object is scoped to the
// job that created it. Seed honeypots always live under a "honeypots/..." object
// key (seed.go's demoHoneypot*Ref, Store.InsertHoneypot's operational seeding);
// aliases always live under a "jobs/..." per-task key. Filtering on that
// documented key-prefix invariant cleanly separates the two. It returns the known
// answer + class alongside the ref so the caller can register the alias in the
// same pass with no second round-trip.
//
// INJECTION-TIME PARAM/MODEL GUARD (byte-exact honeypot safety, docs/DETERMINISM_CLASS.md;
// the guard seed.go's demoHoneypotHawkKnownAnswer doc names as REQUIRED before
// production-scale byte-exact seeding). A byte-exact honeypot's known answer is
// only valid evidence for a job running the EXACT model the answer was captured on
// and at least the max_tokens it was captured under (the hawking seed:
// llama-3.2-1b-instruct-q4, every row EOS'd strictly below max_tokens=24). Keying
// on job_type ALONE would draw such a probe for a batch_infer job on a DIFFERENT
// model, or with a SMALLER max_tokens, where an HONEST same-class worker
// legitimately produces different bytes and would be wrongly quarantined. So a seed
// carrying a non-NULL answer_model is drawn ONLY when the job's model matches AND
// the job's max_tokens is at least the seed's answer_min_max_tokens (a NULL floor
// imposes no minimum). A seed with a NULL answer_model is a tolerant-class probe
// (embed/classification/json/rerank compare semantics, not bytes) with no
// model/param bound — it keeps the old job_type-only behavior. The bounds columns
// are nullable (schema.sql ALTER ... ADD COLUMN IF NOT EXISTS answer_model /
// answer_min_max_tokens), so pre-existing tolerant seeds already read as NULL and
// are unaffected.
func (s *Store) AvailableSeedHoneypots(ctx context.Context, jobType, modelRef string, maxTokens uint32, limit int) ([]SeedHoneypot, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT input_ref, known_answer, COALESCE(answer_class,'')
		   FROM honeypots
		  WHERE job_type = $1
		    AND input_ref NOT LIKE 'jobs/%'
		    AND (answer_model IS NULL OR answer_model = $2)
		    AND (answer_min_max_tokens IS NULL OR answer_min_max_tokens <= $3)
		  ORDER BY created_at ASC
		  LIMIT $4`,
		jobType, modelRef, int64(maxTokens), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SeedHoneypot
	for rows.Next() {
		var h SeedHoneypot
		if err := rows.Scan(&h.InputRef, &h.KnownAnswer, &h.AnswerClass); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// RegisterHoneypotAlias records a per-task OPAQUE input key as a honeypot for
// (jobType, opaqueRef) carrying the SAME known answer + class as the seed
// honeypot it clones, so the verifier's GetHoneypotAnswer(job_type, input_ref)
// lookup — which keys on the task's input_ref, now the opaque key the worker
// sees — still resolves to the real answer (Verification & Result Trust
// 5->5.5). The opaqueRef is a "jobs/{job}/tasks/{taskID}/input.jsonl" key, so
// AvailableSeedHoneypots' "NOT LIKE 'jobs/%'" filter guarantees this alias can
// never be re-dispatched as a honeypot for a future job.
//
// Idempotent (ON no-op if the same opaque key already has a row): a per-task key
// is unique, so a re-run of the same submission path never double-inserts.
// Unlike Store.InsertHoneypot this does NOT run validateHoneypotSeed's
// blank-class refusal: the alias faithfully MIRRORS whatever class the real seed
// honeypot already passed validation under (a byte-exact seed necessarily had a
// non-blank class to exist at all; a tolerant seed is legitimately class-blind),
// so re-validating would be redundant, and refusing a blank class here would
// wrongly reject a tolerant embed honeypot's alias.
func (s *Store) RegisterHoneypotAlias(ctx context.Context, jobType, opaqueRef string, knownAnswer []byte, answerClass string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO honeypots (job_type, input_ref, known_answer, answer_class)
		 SELECT $1, $2, $3, $4
		 WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type=$1 AND input_ref=$2)`,
		jobType, opaqueRef, knownAnswer, answerClass)
	return err
}
