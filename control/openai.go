package main

// openai.go — an OpenAI-compatible Batch API mapped onto the native job pipeline.
//
// Buyers can use OpenAI's batch shape end-to-end:
//
//	POST /v1/files            upload a JSONL of requests        → file object
//	POST /v1/batches          create a batch over that file     → batch object
//	GET  /v1/batches/{id}     poll status                        → batch object
//	GET  /v1/files/{id}/content   download input or output JSONL
//
// Each input line is one inference request keyed by `custom_id`. We translate the
// batch into a native embed / batch_infer job, run it through the SAME scheduler +
// verification + merge as the native API (one source of truth: createJob is reused
// in-process), then translate the merged result back into OpenAI batch output
// JSONL. No new DB tables: file/batch metadata are tiny JSON objects in the object
// store, so the OpenAI surface stays local-first and self-contained.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// maxUploadBytes caps an uploaded batch file (multipart or raw) to keep a single
// request from buffering an unbounded body in memory.
const maxUploadBytes = 64 << 20 // 64 MiB

// fileMeta is the sidecar persisted next to an uploaded/produced JSONL object. It
// carries the owner for buyer-scoped auth and the OpenAI file fields.
type fileMeta struct {
	ID        string `json:"id"`
	BuyerID   string `json:"buyer_id"`
	Purpose   string `json:"purpose"`
	Filename  string `json:"filename"`
	Bytes     int64  `json:"bytes"`
	CreatedAt int64  `json:"created_at"`
}

// batchMeta is the persisted state of one batch: the owning buyer, the native job
// it maps to, the OpenAI endpoint, and the produced output file once generated.
type batchMeta struct {
	ID           string `json:"id"`
	BuyerID      string `json:"buyer_id"`
	JobID        string `json:"job_id"`
	Endpoint     string `json:"endpoint"`
	Model        string `json:"model"`
	InputFileID  string `json:"input_file_id"`
	OutputFileID string `json:"output_file_id"`
	Total        int    `json:"total"`
	CreatedAt    int64  `json:"created_at"`
}

// --- object-store keys (no DB): file/batch ids embed a uuid we strip back out ---

func fileContentKey(id string) string { return "files/" + strings.TrimPrefix(id, "file-") + ".jsonl" }
func fileMetaKey(id string) string    { return "files/" + strings.TrimPrefix(id, "file-") + ".meta" }
func batchMetaKey(id string) string   { return "batches/" + strings.TrimPrefix(id, "batch-") + ".meta" }

// --- POST /v1/files -----------------------------------------------------------

// handleCreateFile stores an uploaded JSONL and returns an OpenAI file object.
// Accepts multipart/form-data (`file` + `purpose`, what the OpenAI SDKs send) or a
// raw JSONL body (`?purpose=` query). The content lands in the object store; a
// `.meta` sidecar records the owner so downloads are buyer-scoped.
func (s *Server) handleCreateFile(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	ctx := r.Context()

	var content []byte
	purpose := "batch"
	filename := "input.jsonl"
	if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/") {
		if err := r.ParseMultipartForm(maxUploadBytes); err != nil {
			writeErr(w, http.StatusBadRequest, "parsing multipart upload: "+err.Error())
			return
		}
		f, hdr, err := r.FormFile("file")
		if err != nil {
			writeErr(w, http.StatusBadRequest, "missing `file` part: "+err.Error())
			return
		}
		defer f.Close()
		content, err = io.ReadAll(io.LimitReader(f, maxUploadBytes))
		if err != nil {
			writeErr(w, http.StatusBadRequest, "reading uploaded file: "+err.Error())
			return
		}
		if p := r.FormValue("purpose"); p != "" {
			purpose = p
		}
		if hdr != nil && hdr.Filename != "" {
			filename = hdr.Filename
		}
	} else {
		b, err := io.ReadAll(io.LimitReader(r.Body, maxUploadBytes))
		if err != nil {
			writeErr(w, http.StatusBadRequest, "reading body: "+err.Error())
			return
		}
		content = b
		if p := r.URL.Query().Get("purpose"); p != "" {
			purpose = p
		}
	}
	if len(bytes.TrimSpace(content)) == 0 {
		writeErr(w, http.StatusBadRequest, "uploaded file is empty")
		return
	}

	id := "file-" + uuid.NewString()
	if err := s.storage.PutObject(ctx, fileContentKey(id), content, "application/x-ndjson"); err != nil {
		writeErr(w, http.StatusInternalServerError, "storing file: "+err.Error())
		return
	}
	meta := fileMeta{
		ID: id, BuyerID: auth.BuyerID.String(), Purpose: purpose,
		Filename: filename, Bytes: int64(len(content)), CreatedAt: time.Now().Unix(),
	}
	if err := s.putMeta(ctx, fileMetaKey(id), meta); err != nil {
		writeErr(w, http.StatusInternalServerError, "storing file meta: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, fileObject(meta))
}

// --- POST /v1/batches ---------------------------------------------------------

// handleCreateBatch creates a batch over an uploaded file: it parses the OpenAI
// batch input, translates it into a native job, submits that job through the SAME
// pipeline as POST /v1/jobs (createJob, in-process), and records the mapping.
func (s *Server) handleCreateBatch(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	ctx := r.Context()

	var req struct {
		InputFileID      string          `json:"input_file_id"`
		Endpoint         string          `json:"endpoint"`
		CompletionWindow string          `json:"completion_window"`
		Input            json.RawMessage `json:"input"` // inline alternative to input_file_id
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid batch request json: "+err.Error())
		return
	}
	jobType, ok := endpointJobType(req.Endpoint)
	if !ok {
		writeErr(w, http.StatusBadRequest, "unsupported endpoint "+req.Endpoint+" (use /v1/embeddings or /v1/chat/completions)")
		return
	}

	// Two honest ways to supply the request lines: a previously-uploaded file
	// (`input_file_id`), OR an inline `input` for callers who skip the upload step.
	// Inline is the raw JSONL (a JSON string) or a JSON array of OpenAI batch line
	// objects; either way it is materialized into the SAME buyer-owned input file the
	// file path produces, so the rest of the flow (parse, submit, output) is identical.
	var inputFileID string
	switch {
	case req.InputFileID != "":
		fm, herr := s.getFileMeta(ctx, req.InputFileID, auth.BuyerID)
		if herr != nil {
			writeErr(w, herr.status, herr.msg)
			return
		}
		inputFileID = fm.ID
	case len(req.Input) > 0:
		jsonl, ierr := inlineBatchJSONL(req.Input)
		if ierr != nil {
			writeErr(w, http.StatusBadRequest, "invalid inline batch input: "+ierr.Error())
			return
		}
		id := "file-" + uuid.NewString()
		if err := s.storage.PutObject(ctx, fileContentKey(id), jsonl, "application/x-ndjson"); err != nil {
			writeErr(w, http.StatusInternalServerError, "storing inline input: "+err.Error())
			return
		}
		fm := fileMeta{
			ID: id, BuyerID: auth.BuyerID.String(), Purpose: "batch",
			Filename: "inline.jsonl", Bytes: int64(len(jsonl)), CreatedAt: time.Now().Unix(),
		}
		if err := s.putMeta(ctx, fileMetaKey(id), fm); err != nil {
			writeErr(w, http.StatusInternalServerError, "storing inline input meta: "+err.Error())
			return
		}
		inputFileID = id
	default:
		writeErr(w, http.StatusBadRequest, "batch requires input_file_id or inline input")
		return
	}

	raw, err := s.storage.GetObject(ctx, fileContentKey(inputFileID))
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "reading input file: "+err.Error())
		return
	}
	customIDs, nativeJSONL, model, perr := parseBatchInput(raw, jobType)
	if perr != nil {
		writeErr(w, http.StatusBadRequest, "invalid batch input: "+perr.Error())
		return
	}

	// Reuse the native submission pipeline verbatim (one source of truth): a JSON
	// string IS the inline JSONL that resolveInput expects.
	inputField, _ := json.Marshal(string(nativeJSONL))
	// LaunchContract (items 1-5): the OpenAI Batch wire format carries NO CX contract
	// fields (max_usd / private_pool / min_reputation / verification), so a batch job
	// cannot express a per-job contract — a NAMED external-format dependency, not a drop
	// we can fix here. It still runs under createJob's account-level guards (the
	// free-credit spend cap applies). A future CX extension could read a cap from the
	// batch `metadata`; until then this path is intentionally contract-less.
	resp, herr := s.createJob(ctx, auth.BuyerID, jobSubmit{
		JobType: JobType{Type: jobType},
		Model:   ModelRef{Kind: "gguf", Ref: model},
		Tier:    "batch",
		Input:   inputField,
	})
	if herr != nil {
		writeErr(w, herr.status, "submitting batch job: "+herr.msg)
		return
	}

	id := "batch-" + uuid.NewString()
	meta := batchMeta{
		ID: id, BuyerID: auth.BuyerID.String(), JobID: resp.JobID.String(),
		Endpoint: req.Endpoint, Model: model, InputFileID: inputFileID,
		Total: len(customIDs), CreatedAt: time.Now().Unix(),
	}
	if err := s.putMeta(ctx, batchMetaKey(id), meta); err != nil {
		writeErr(w, http.StatusInternalServerError, "storing batch meta: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, batchObject(meta, "in_progress", 0))
}

// --- GET /v1/batches/{id} -----------------------------------------------------

// handleGetBatch maps the underlying job's status to the OpenAI batch status, and
// — once the job completes — lazily generates the output file (translating the
// merged native result into OpenAI batch output JSONL) and records its id.
func (s *Server) handleGetBatch(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	ctx := r.Context()
	id := r.PathValue("id")

	var meta batchMeta
	if err := s.getMeta(ctx, batchMetaKey(id), &meta); err != nil {
		writeErr(w, http.StatusNotFound, "batch not found")
		return
	}
	if meta.BuyerID != auth.BuyerID.String() {
		writeErr(w, http.StatusForbidden, "batch belongs to another buyer")
		return
	}

	jobID, err := uuid.Parse(meta.JobID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "corrupt batch job id")
		return
	}
	j, err := s.store.GetJob(ctx, jobID, auth.BuyerID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading batch job: "+err.Error())
		return
	}

	status, completed := batchStatus(j.Status, meta.Total, j.TasksDone)
	if j.Status == "complete" && meta.OutputFileID == "" {
		outID, gerr := s.generateBatchOutput(ctx, &meta, j.OutputRef)
		if gerr != nil {
			writeErr(w, http.StatusInternalServerError, "generating batch output: "+gerr.Error())
			return
		}
		meta.OutputFileID = outID
		if err := s.putMeta(ctx, batchMetaKey(id), meta); err != nil {
			writeErr(w, http.StatusInternalServerError, "recording batch output: "+err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, batchObject(meta, status, completed))
}

// --- GET /v1/files/{id}/content -----------------------------------------------

// handleGetFileContent streams a file's JSONL body (input or generated output),
// scoped to the owning buyer.
func (s *Server) handleGetFileContent(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	ctx := r.Context()
	id := r.PathValue("id")

	if _, herr := s.getFileMeta(ctx, id, auth.BuyerID); herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	body, err := s.storage.GetObject(ctx, fileContentKey(id))
	if err != nil {
		writeErr(w, http.StatusNotFound, "file content not found")
		return
	}
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	w.Write(body)
}

// --- translation + helpers ----------------------------------------------------

// endpointJobType maps an OpenAI endpoint to a native job type.
func endpointJobType(endpoint string) (string, bool) {
	switch endpoint {
	case "/v1/embeddings":
		return "embed", true
	case "/v1/chat/completions":
		return "batch_infer", true
	default:
		return "", false
	}
}

// mapModel resolves the OpenAI model name (or a native id) to our catalogue id for
// the given job type. Unknown/blank names fall back to the canonical model.
func mapModel(jobType, openaiModel string) string {
	m := strings.ToLower(strings.TrimSpace(openaiModel))
	if jobType == "embed" {
		if m == "all-minilm-l6-v2" {
			return m
		}
		return "all-minilm-l6-v2" // text-embedding-3-*, ada-002, … → our embed model
	}
	if m == "llama-3.2-1b-instruct-q4" {
		return m
	}
	return "llama-3.2-1b-instruct-q4" // gpt-*, o*, … → our batch_infer model
}

// batchInputLine is one OpenAI batch request line.
type batchInputLine struct {
	CustomID string `json:"custom_id"`
	URL      string `json:"url"`
	Body     struct {
		Model    string          `json:"model"`
		Input    json.RawMessage `json:"input"` // embeddings: string or [string]
		Messages []struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"messages"` // chat
	} `json:"body"`
}

// inlineBatchJSONL normalizes an inline batch `input` into OpenAI batch JSONL bytes
// (the same shape an uploaded file holds), so the inline and file paths converge.
// Accepts either a JSON string (already-formed JSONL) or a JSON array of line
// objects (each marshaled to its own line). Anything else is an honest error.
func inlineBatchJSONL(input json.RawMessage) ([]byte, error) {
	var asString string
	if json.Unmarshal(input, &asString) == nil {
		if strings.TrimSpace(asString) == "" {
			return nil, fmt.Errorf("inline input string is empty")
		}
		return []byte(asString), nil
	}
	var asArray []json.RawMessage
	if json.Unmarshal(input, &asArray) == nil {
		if len(asArray) == 0 {
			return nil, fmt.Errorf("inline input array is empty")
		}
		var buf bytes.Buffer
		for _, line := range asArray {
			buf.Write(bytes.TrimSpace(line))
			buf.WriteByte('\n')
		}
		return buf.Bytes(), nil
	}
	return nil, fmt.Errorf("input must be a JSONL string or an array of request objects")
}

// parseBatchInput reads OpenAI batch JSONL into ordered custom_ids and a native
// job input JSONL (one {"id","text"} per line). Returns the resolved model id. A
// malformed or empty input is an error (never a silent partial job).
func parseBatchInput(raw []byte, jobType string) (customIDs []string, nativeJSONL []byte, model string, err error) {
	var buf bytes.Buffer
	openaiModel := ""
	for i, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var in batchInputLine
		if e := json.Unmarshal([]byte(line), &in); e != nil {
			return nil, nil, "", fmt.Errorf("line %d: %w", i+1, e)
		}
		cid := in.CustomID
		if cid == "" {
			cid = fmt.Sprintf("request-%d", len(customIDs))
		}
		text, e := extractText(jobType, &in)
		if e != nil {
			return nil, nil, "", fmt.Errorf("line %d (%s): %w", i+1, cid, e)
		}
		if openaiModel == "" {
			openaiModel = in.Body.Model
		}
		rec, _ := json.Marshal(map[string]string{"id": cid, "text": text})
		buf.Write(rec)
		buf.WriteByte('\n')
		customIDs = append(customIDs, cid)
	}
	if len(customIDs) == 0 {
		return nil, nil, "", fmt.Errorf("no request lines found")
	}
	return customIDs, buf.Bytes(), mapModel(jobType, openaiModel), nil
}

// extractText pulls the inference text from one request line: the embeddings
// `input` (string, or first element of a string array) or the last chat message.
func extractText(jobType string, in *batchInputLine) (string, error) {
	if jobType == "embed" {
		var s string
		if json.Unmarshal(in.Body.Input, &s) == nil {
			return s, nil
		}
		var arr []string
		if json.Unmarshal(in.Body.Input, &arr) == nil && len(arr) > 0 {
			return arr[0], nil
		}
		return "", fmt.Errorf("embeddings body.input must be a string or [string]")
	}
	// chat: take the last message's content (the prompt).
	for i := len(in.Body.Messages) - 1; i >= 0; i-- {
		if c := in.Body.Messages[i].Content; c != "" {
			return c, nil
		}
	}
	return "", fmt.Errorf("chat body.messages is empty")
}

// generateBatchOutput translates the job's merged result into OpenAI batch output
// JSONL, stores it as a new (buyer-owned) output file, and returns its id.
func (s *Server) generateBatchOutput(ctx context.Context, meta *batchMeta, outputRef string) (string, error) {
	if _, err := s.MergeJobResults(ctx, meta.toJobID()); err != nil {
		return "", fmt.Errorf("merging results: %w", err)
	}
	merged, err := s.storage.GetObject(ctx, outputRef)
	if err != nil {
		return "", fmt.Errorf("reading merged output: %w", err)
	}
	input, err := s.storage.GetObject(ctx, fileContentKey(meta.InputFileID))
	if err != nil {
		return "", fmt.Errorf("reading input for custom_ids: %w", err)
	}
	customIDs, _, _, perr := parseBatchInput(input, jobTypeFromEndpoint(meta.Endpoint))
	if perr != nil {
		return "", perr
	}
	out, berr := buildBatchOutput(meta.Endpoint, meta.Model, customIDs, merged)
	if berr != nil {
		return "", berr
	}

	id := "file-" + uuid.NewString()
	if err := s.storage.PutObject(ctx, fileContentKey(id), out, "application/x-ndjson"); err != nil {
		return "", err
	}
	fmeta := fileMeta{
		ID: id, BuyerID: meta.BuyerID, Purpose: "batch_output",
		Filename: "output.jsonl", Bytes: int64(len(out)), CreatedAt: time.Now().Unix(),
	}
	if err := s.putMeta(ctx, fileMetaKey(id), fmeta); err != nil {
		return "", err
	}
	return id, nil
}

// buildBatchOutput zips the merged native result (one item per line, in input
// order) with the input custom_ids into OpenAI batch output JSONL. Embeddings
// produce a full `embedding` response body; chat wraps the completion text.
func buildBatchOutput(endpoint, model string, customIDs []string, merged []byte) ([]byte, error) {
	var buf bytes.Buffer
	i := 0
	for _, line := range strings.Split(string(merged), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if i >= len(customIDs) {
			break // never emit more outputs than requests
		}
		body, err := outputBody(endpoint, model, line)
		if err != nil {
			return nil, fmt.Errorf("item %d: %w", i, err)
		}
		rec, _ := json.Marshal(map[string]any{
			"id":        fmt.Sprintf("batch_req_%d", i),
			"custom_id": customIDs[i],
			"response":  map[string]any{"status_code": 200, "request_id": fmt.Sprintf("req_%d", i), "body": body},
			"error":     nil,
		})
		buf.Write(rec)
		buf.WriteByte('\n')
		i++
	}
	return buf.Bytes(), nil
}

// outputBody builds the OpenAI response body for one merged result line.
func outputBody(endpoint, model, line string) (map[string]any, error) {
	if endpoint == "/v1/embeddings" {
		var e struct {
			Vector []float64 `json:"vector"`
		}
		if err := json.Unmarshal([]byte(line), &e); err != nil || len(e.Vector) == 0 {
			return nil, fmt.Errorf("expected an embed result with a vector")
		}
		return map[string]any{
			"object": "list",
			"data":   []any{map[string]any{"object": "embedding", "index": 0, "embedding": e.Vector}},
			"model":  model,
			"usage":  map[string]any{"prompt_tokens": 0, "total_tokens": 0},
		}, nil
	}
	// chat: the merged line is the per-item record; surface its text as the message.
	var rec map[string]any
	_ = json.Unmarshal([]byte(line), &rec)
	content := ""
	for _, k := range []string{"text", "completion", "output", "response"} {
		if v, ok := rec[k].(string); ok {
			content = v
			break
		}
	}
	return map[string]any{
		"object":  "chat.completion",
		"model":   model,
		"choices": []any{map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": content}, "finish_reason": "stop"}},
		"usage":   map[string]any{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
	}, nil
}

func jobTypeFromEndpoint(endpoint string) string {
	jt, _ := endpointJobType(endpoint)
	return jt
}

func (m *batchMeta) toJobID() uuid.UUID {
	id, _ := uuid.Parse(m.JobID)
	return id
}

// batchStatus maps a native job status onto the OpenAI batch status vocabulary and
// the completed count for request_counts.
func batchStatus(jobStatus string, total, tasksDone int) (string, int) {
	switch jobStatus {
	case "complete":
		return "completed", total
	case "failed":
		return "failed", 0
	case "cancelled":
		return "cancelled", 0
	case "verifying":
		return "finalizing", min(tasksDone, total)
	default: // queued | running
		return "in_progress", min(tasksDone, total)
	}
}

// fileObject / batchObject render the OpenAI-shaped JSON responses.
func fileObject(m fileMeta) map[string]any {
	return map[string]any{
		"id": m.ID, "object": "file", "bytes": m.Bytes, "created_at": m.CreatedAt,
		"filename": m.Filename, "purpose": m.Purpose, "status": "processed",
	}
}

func batchObject(m batchMeta, status string, completed int) map[string]any {
	var outID any
	if m.OutputFileID != "" {
		outID = m.OutputFileID
	}
	return map[string]any{
		"id": m.ID, "object": "batch", "endpoint": m.Endpoint,
		"input_file_id": m.InputFileID, "output_file_id": outID,
		"completion_window": "24h", "status": status,
		"created_at":     m.CreatedAt,
		"request_counts": map[string]any{"total": m.Total, "completed": completed, "failed": 0},
	}
}

// --- object-store metadata + buyer-scoped lookup ------------------------------

func (s *Server) putMeta(ctx context.Context, key string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return s.storage.PutObject(ctx, key, b, "application/json")
}

func (s *Server) getMeta(ctx context.Context, key string, v any) error {
	b, err := s.storage.GetObject(ctx, key)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

// getFileMeta loads a file's metadata and enforces buyer ownership.
func (s *Server) getFileMeta(ctx context.Context, id string, buyerID uuid.UUID) (*fileMeta, *httpError) {
	if !strings.HasPrefix(id, "file-") {
		return nil, &httpError{http.StatusBadRequest, "invalid file id"}
	}
	var m fileMeta
	if err := s.getMeta(ctx, fileMetaKey(id), &m); err != nil {
		return nil, &httpError{http.StatusNotFound, "file not found"}
	}
	if m.BuyerID != buyerID.String() {
		return nil, &httpError{http.StatusForbidden, "file belongs to another buyer"}
	}
	return &m, nil
}
