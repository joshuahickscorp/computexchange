package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/google/uuid"
)

// intake.go — the Concierge. Connect a source (a GitHub repo or an uploaded file
// listing), inspect it, and AUTO-DETECT the pipeline of CX workloads it needs.
// The goal is to collapse buyer input to "connect → approve": the system
// recognizes the data shape, picks the workloads + models, and builds the job
// plan — the buyer never hand-writes JSONL or chooses a job type. What is genuinely
// external (a real GitHub OAuth app) is gated on credentials and surfaces an honest
// error rather than faking a connection (BLACKHOLE: surface every failure).

// --- detected pipeline (buyer-facing) ---

// PipelineStage is one detected step. Op is a CX job type (embed /
// batch_classification / audio_transcribe / json_extraction / …); Detail explains
// what it operates on and why it was detected — that transparency is the whole
// point of "show the user what pipeline we detected".
type PipelineStage struct {
	Op     string `json:"op"`
	Model  string `json:"model"`
	Detail string `json:"detail"`
	// From selects this stage's input: "" / "input" runs on the source data (the
	// fan-out the current text patterns use — embed AND classify both run on the
	// text); "previous" chains it onto the prior stage's output (e.g. transcribe →
	// summarize), submitted by advanceIntake when the predecessor completes.
	From string `json:"from,omitempty"`
}

// DetectedPipeline is the result of inspecting a source. Supported=false is honest:
// the Concierge refuses a shape it cannot map yet (with a Reason) instead of
// guessing a plan that would burn the buyer's money on the wrong work.
type DetectedPipeline struct {
	Pattern   string `json:"pattern"`
	Supported bool   `json:"supported"`
	// Launchable is true only when the pattern can actually be EXTRACTED and launched
	// from a connected repo today (see patternLaunchable). A workload can be recognized
	// (Supported) yet not launchable from a repo — e.g. audio transcription, whose input
	// is binary audio via the upload path, not a repo fetch. The UI offers Launch only
	// when Launchable; the launch handler refuses otherwise (item 20).
	Launchable bool `json:"launchable"`
	// Truncated is true when the source file listing was INCOMPLETE (GitHub truncated
	// the recursive tree), so detection ran over a PARTIAL listing and may be wrong.
	// The buyer is warned and should review before launch (item 18).
	Truncated bool            `json:"truncated,omitempty"`
	Reason    string          `json:"reason,omitempty"`
	Stages    []PipelineStage `json:"stages"`
}

// GitSource is a connected source (returned by the store, serialized to the buyer
// WITHOUT its access token).
type GitSource struct {
	ID            uuid.UUID `json:"id"`
	Provider      string    `json:"provider"`
	RepoFullName  string    `json:"repo_full_name"`
	DefaultBranch string    `json:"default_branch"`
	AccessToken   string    `json:"-"`
	ConnectedAt   time.Time `json:"connected_at"`
}

// --- the pattern catalogue (the automation brain) ---

// RepoFile is the minimal view a detector needs: a path and a size. This is what
// makes detection PURE — it runs on a listing, no matter whether that listing came
// from GitHub or an upload, so it is identical in production and in a unit test.
type RepoFile struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
}

// intakePattern is one recognizer. match returns true + buyer-facing evidence when
// the listing fits; build returns the stages to run. Patterns are tried in order,
// first match wins. Adding a new supported workload = adding ONE pattern here and
// nothing else — that is the seam that lets the system "handle all of it".
type intakePattern struct {
	key   string
	match func(files []RepoFile) (bool, string)
	build func(evidence string) []PipelineStage
}

func countExt(files []RepoFile, exts ...string) (int, string) {
	n, first := 0, ""
	for _, f := range files {
		l := strings.ToLower(f.Path)
		for _, e := range exts {
			if strings.HasSuffix(l, e) {
				n++
				if first == "" {
					first = f.Path
				}
				break
			}
		}
	}
	return n, first
}

// documentSetExts is the SINGLE source of truth for which files the document-set
// pattern recognizes AND extracts. Detection (the match below) and extraction
// (extractInput) MUST use the same set, or a repo gets "supported then 0 records"
// (the old bug: .pdf matched but was never fetched). PDF is not extractable yet, so
// it is deliberately absent — a PDF-only repo is honestly unsupported (item 19).
var documentSetExts = []string{".md", ".txt", ".html"}

// codeRepoExts is the shared source of truth for the code-repo pattern: detection AND
// extraction agree (like documentSetExts), so a matched source file is always fetched.
// Common source extensions across Go/Rust/TS/JS/Python/Java/C/C++/etc. (item 21).
var codeRepoExts = []string{
	".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".rb",
	".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".php", ".scala",
}

// tabularExts is the shared source of truth for the tabular pattern (detect + extract).
var tabularExts = []string{".csv", ".jsonl", ".tsv"}

// ExtractionStats reports how a source listing became job input (item 23): files that
// matched the pattern, files actually used (fetched + extracted), files skipped
// (everything else in the listing), JSONL records produced, and fetched bytes. Surfaced
// in the intake launch response so the buyer sees what was used vs left out.
type ExtractionStats struct {
	FilesMatched int   `json:"files_matched"`
	FilesUsed    int   `json:"files_used"`
	FilesSkipped int   `json:"files_skipped"`
	Records      int   `json:"records"`
	Bytes        int64 `json:"bytes"`
}

// docStats computes ExtractionStats for a fetch-and-chunk extraction. Pure — unit-tested.
func docStats(allFiles []RepoFile, matchExts []string, used []namedContent, records int) ExtractionStats {
	matched, _ := countExt(allFiles, matchExts...)
	var b int64
	for _, d := range used {
		b += int64(len(d.Content))
	}
	return ExtractionStats{
		FilesMatched: matched,
		FilesUsed:    len(used),
		FilesSkipped: len(allFiles) - len(used),
		Records:      records,
		Bytes:        b,
	}
}

// Intake read caps bound untrusted GitHub content so one file (or one repo) cannot
// exhaust control-plane memory (items 16-17). Exceeding either returns the typed
// errIntakeTooLarge, which the intake handlers surface as a 4xx rather than an OOM.
const (
	maxIntakeFileBytes  = 25 << 20  // 25 MiB per file
	maxIntakeTotalBytes = 200 << 20 // 200 MiB aggregate per extract
)

// errIntakeTooLarge is the typed sentinel for an over-cap intake read (per-file or
// aggregate). Callers check it with errors.Is.
var errIntakeTooLarge = fmt.Errorf("intake input too large")

// intakePatterns is the ordered catalogue. Start narrow and honest; widen over
// time. Each entry is the full "support" the system gives a workload: how to
// recognize its data and how to turn it into real CX jobs.
var intakePatterns = []intakePattern{
	{
		key: "audio-transcribe",
		match: func(files []RepoFile) (bool, string) {
			if n, _ := countExt(files, ".wav", ".mp3", ".m4a", ".flac"); n > 0 {
				return true, fmt.Sprintf("%d audio files", n)
			}
			return false, ""
		},
		build: func(ev string) []PipelineStage {
			return []PipelineStage{{Op: "audio_transcribe", Model: "whisper-tiny", Detail: ev + " → transcripts"}}
		},
	},
	{
		key: "tabular-text",
		match: func(files []RepoFile) (bool, string) {
			if n, p := countExt(files, tabularExts...); n > 0 {
				return true, "tabular text · " + p
			}
			return false, ""
		},
		build: func(ev string) []PipelineStage {
			return []PipelineStage{
				{Op: "embed", Model: "all-minilm-l6-v2", Detail: ev + " → 384-dim vectors"},
				{Op: "batch_classification", Model: "llama-3.2-1b-instruct-q4", Detail: "topic label per row"},
			}
		},
	},
	{
		key: "document-set",
		match: func(files []RepoFile) (bool, string) {
			if n, _ := countExt(files, documentSetExts...); n >= 3 {
				return true, fmt.Sprintf("%d documents", n)
			}
			return false, ""
		},
		build: func(ev string) []PipelineStage {
			return []PipelineStage{{Op: "json_extraction", Model: "llama-3.2-1b-instruct-q4", Detail: ev + " → structured JSON"}}
		},
	},
	{
		// code-repo: a source-code corpus (>=2 source files) → a deterministic chunked
		// embed index. The most common shape we used to refuse (item 21). Tried AFTER the
		// data patterns so a repo that is really tabular/docs/audio still matches those first.
		key: "code-repo",
		match: func(files []RepoFile) (bool, string) {
			if n, _ := countExt(files, codeRepoExts...); n >= 2 {
				return true, fmt.Sprintf("%d source files", n)
			}
			return false, ""
		},
		build: func(ev string) []PipelineStage {
			return []PipelineStage{{Op: "embed", Model: "all-minilm-l6-v2", Detail: ev + " → chunked 384-dim code index"}}
		},
	},
}

// detectPipeline runs the catalogue over a file listing. PURE — no I/O — so it is
// the same code path for a GitHub repo and an upload, and unit-testable without
// either. An unmatched listing returns Supported=false with an honest Reason; it
// never fabricates a plan.
func detectPipeline(files []RepoFile) DetectedPipeline {
	for _, p := range intakePatterns {
		if ok, ev := p.match(files); ok {
			return DetectedPipeline{
				Pattern:    p.key,
				Supported:  true,
				Launchable: patternLaunchable(p.key),
				Stages:     p.build(ev),
			}
		}
	}
	return DetectedPipeline{
		Pattern:   "unknown",
		Supported: false,
		Reason:    "no known data pattern detected — supported today: audio (transcribe), tabular text (embed + classify), document sets (extract). Connect data that matches, or choose a workload manually.",
	}
}

// patternLaunchable reports whether a detected pattern can actually be EXTRACTED and
// launched from a connected repo today. A pattern is launchable only if extractInput
// has a real extractor for it. audio-transcribe is RECOGNIZED but NOT launchable from
// a repo (its input is binary audio via the upload path, not a repo fetch), so the
// launch handler refuses it EARLY instead of fetching the source and failing
// mid-extract (item 20). Pure — unit-tested, and keyed on the persisted pattern name
// so old persisted intakes gate correctly too.
func patternLaunchable(pattern string) bool {
	switch pattern {
	case "tabular-text", "document-set", "code-repo":
		return true
	default:
		return false // audio-transcribe + unknown: not launchable from a repo
	}
}

// withTruncationWarning marks a detection as made over an INCOMPLETE listing and
// appends an honest warning to its reason, so a truncated repo never yields a
// confidently-wrong plan (item 18). Pure — unit-tested.
func withTruncationWarning(det DetectedPipeline) DetectedPipeline {
	det.Truncated = true
	det.Reason = strings.TrimSpace(det.Reason + " NOTE: the repository file listing was truncated by GitHub (too many files); detection may be incomplete — review before launch.")
	return det
}

// --- GitHub connection (real HTTP, gated on a configured OAuth app) ---

var errGitHubUnconfigured = fmt.Errorf("github connect is not configured (set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET) — connect is disabled, never faked")

// GitHubApp is the OAuth + read client. Configured() is false until a real GitHub
// OAuth App's credentials are present in the environment; every method then returns
// errGitHubUnconfigured rather than pretending a repo was read.
type GitHubApp struct {
	clientID, clientSecret, redirect string
	http                             *http.Client
}

func newGitHubApp() *GitHubApp {
	return &GitHubApp{
		clientID:     os.Getenv("GITHUB_CLIENT_ID"),
		clientSecret: os.Getenv("GITHUB_CLIENT_SECRET"),
		redirect:     os.Getenv("GITHUB_REDIRECT_URL"),
		http:         &http.Client{Timeout: 20 * time.Second},
	}
}

// githubApp is the process-wide client, built from the environment at load (like
// the payout rail's selection). No credentials → an honest, disabled connector.
var githubApp = newGitHubApp()

func (g *GitHubApp) Configured() bool { return g.clientID != "" && g.clientSecret != "" }

// AuthURL is the GitHub authorize URL to redirect the buyer to (repo read scope).
func (g *GitHubApp) AuthURL(state string) (string, error) {
	if !g.Configured() {
		return "", errGitHubUnconfigured
	}
	q := url.Values{"client_id": {g.clientID}, "redirect_uri": {g.redirect}, "scope": {"repo"}, "state": {state}}
	return "https://github.com/login/oauth/authorize?" + q.Encode(), nil
}

// Exchange swaps an OAuth code for an access token.
func (g *GitHubApp) Exchange(ctx context.Context, code string) (string, error) {
	if !g.Configured() {
		return "", errGitHubUnconfigured
	}
	form := url.Values{"client_id": {g.clientID}, "client_secret": {g.clientSecret}, "code": {code}}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "https://github.com/login/oauth/access_token", strings.NewReader(form.Encode()))
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := g.http.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out struct {
		AccessToken string `json:"access_token"`
		ErrorDesc   string `json:"error_description"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("github token exchange: unparseable response")
	}
	if out.AccessToken == "" {
		return "", fmt.Errorf("github token exchange failed: %s", out.ErrorDesc)
	}
	return out.AccessToken, nil
}

// Tree lists a repo's files at a ref via the recursive git-trees API. The second
// return is GitHub's `truncated` flag: when true the listing is INCOMPLETE (the repo
// has more entries than one tree response carries), so detection over it is partial
// and the caller marks the result low-confidence rather than trusting it (item 18).
func (g *GitHubApp) Tree(ctx context.Context, token, repoFullName, ref string) ([]RepoFile, bool, error) {
	if !g.Configured() {
		return nil, false, errGitHubUnconfigured
	}
	u := fmt.Sprintf("https://api.github.com/repos/%s/git/trees/%s?recursive=1", repoFullName, url.PathEscape(ref))
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, false, fmt.Errorf("github tree (%d): %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out struct {
		Tree []struct {
			Path string `json:"path"`
			Type string `json:"type"`
			Size int64  `json:"size"`
		} `json:"tree"`
		Truncated bool `json:"truncated"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, false, err
	}
	files := make([]RepoFile, 0, len(out.Tree))
	for _, t := range out.Tree {
		if t.Type == "blob" {
			files = append(files, RepoFile{Path: t.Path, Size: t.Size})
		}
	}
	return files, out.Truncated, nil
}

// --- handlers ---

// handleGithubConnect starts OAuth: returns the GitHub authorize URL the app
// redirects the buyer to (503 with an honest reason if no OAuth app is configured).
func (s *Server) handleGithubConnect(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	// state carries the buyer back to the callback. PRODUCTION: this must be a
	// signed/random nonce bound to the session to stop CSRF — noted, not faked.
	u, err := githubApp.AuthURL(signState(auth.BuyerID))
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"authorize_url": u})
}

// handleGithubCallback completes OAuth: exchanges the code for a token and stores
// the connection. Unauthed (GitHub redirects here with no bearer); the buyer is
// recovered from state. Real only with a configured app; otherwise an honest 503.
func (s *Server) handleGithubCallback(w http.ResponseWriter, r *http.Request) {
	buyerID, ok := verifyState(r.URL.Query().Get("state"))
	if !ok {
		writeErr(w, http.StatusBadRequest, "bad or missing OAuth state")
		return
	}
	token, err := githubApp.Exchange(r.Context(), r.URL.Query().Get("code"))
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	id, err := s.store.InsertGitSource(r.Context(), buyerID, token)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"source_id": id.String(), "status": "connected"})
}

// handleListSources lists the buyer's connected sources.
func (s *Server) handleListSources(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	srcs, err := s.store.ListGitSources(r.Context(), auth.BuyerID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"sources": srcs})
}

// intakeRequest drives detection from either a connected source (source_id + repo
// + ref → fetched from GitHub) OR a direct file listing (the upload path, also how
// a test drives detection without GitHub).
type intakeRequest struct {
	SourceID string     `json:"source_id"`
	Repo     string     `json:"repo"`
	Ref      string     `json:"ref"`
	Files    []RepoFile `json:"files"`
}

// handleCreateIntake is the heart of "the system handles it": inspect → detect.
// It returns the detected pipeline so the app can render exactly what we recognized.
// An unsupported shape is a valid answer (HTTP 200, supported:false), not an error —
// the buyer learns honestly that we can't run it yet.
func (s *Server) handleCreateIntake(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var req intakeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid intake body")
		return
	}
	files := req.Files
	var truncated bool
	// A connected source → list its files from GitHub (real only with a configured
	// app + stored token; otherwise an honest 503).
	if len(files) == 0 && req.SourceID != "" {
		src, err := s.store.GetGitSource(r.Context(), auth.BuyerID, req.SourceID)
		if err != nil {
			writeErr(w, http.StatusNotFound, "source not found")
			return
		}
		repo := req.Repo
		if repo == "" {
			repo = src.RepoFullName
		}
		ref := req.Ref
		if ref == "" {
			ref = "HEAD"
		}
		files, truncated, err = githubApp.Tree(r.Context(), src.AccessToken, repo, ref)
		if err != nil {
			writeErr(w, http.StatusServiceUnavailable, err.Error())
			return
		}
	}
	if len(files) == 0 {
		writeErr(w, http.StatusBadRequest, "no files to inspect (provide a connected source_id, or a files listing)")
		return
	}
	det := detectPipeline(files)
	if truncated {
		// Incomplete listing — mark + warn so the buyer never trusts a detection made
		// over a partial repo (item 18). We warn rather than refuse: a truncated listing
		// is often still enough to recognize the shape.
		det = withTruncationWarning(det)
	}
	status := "detected"
	if !det.Supported {
		status = "unsupported"
	}
	pj, _ := json.Marshal(det)
	id, err := s.store.InsertIntake(r.Context(), auth.BuyerID, req.SourceID, req.Ref, status, det.Pattern, pj)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"intake_id": id.String(), "pipeline": det})
}

// --- repo picker + raw read ---

// RepoRef is a repo the buyer can pick after connecting (most-recently-pushed first).
type RepoRef struct {
	FullName      string `json:"full_name"`
	DefaultBranch string `json:"default_branch"`
	Private       bool   `json:"private"`
}

// ListRepos lists the connected account's repos for the picker.
func (g *GitHubApp) ListRepos(ctx context.Context, token string) ([]RepoRef, error) {
	if !g.Configured() {
		return nil, errGitHubUnconfigured
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.github.com/user/repos?per_page=100&sort=pushed", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("github repos (%d): %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var out []RepoRef
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

// RawFile fetches one file's bytes at a ref via the contents API (raw media type).
func (g *GitHubApp) RawFile(ctx context.Context, token, repo, ref, path string) ([]byte, error) {
	if !g.Configured() {
		return nil, errGitHubUnconfigured
	}
	u := fmt.Sprintf("https://api.github.com/repos/%s/contents/%s?ref=%s", repo, path, url.QueryEscape(ref))
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github.raw")
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("github raw %s (%d): %s", path, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	// Cap the untrusted raw read so one file cannot exhaust control-plane memory (item 16).
	return readCapped(resp.Body, maxIntakeFileBytes, path)
}

// readCapped reads at most `max` bytes from r, returning the typed errIntakeTooLarge
// if the source exceeds `max`. It reads ONE byte past the cap so it DETECTS an
// oversize file rather than truncating it silently. Pure — unit-tested without GitHub.
func readCapped(r io.Reader, max int64, path string) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > max {
		return nil, fmt.Errorf("%w: %s exceeds %d bytes", errIntakeTooLarge, path, max)
	}
	return b, nil
}

// handleListRepos returns the repos for a connected source (the picker).
func (s *Server) handleListRepos(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	src, err := s.store.GetGitSource(r.Context(), auth.BuyerID, r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusNotFound, "source not found")
		return
	}
	repos, err := githubApp.ListRepos(r.Context(), src.AccessToken)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"repos": repos})
}

// --- launch: the automation payoff (fetch → extract → submit) ---

type launchRequest struct {
	IntakeID string `json:"intake_id"`
	Repo     string `json:"repo"`
	Ref      string `json:"ref"`
	// LaunchContract fields (items 1-5): the budget/verification/routing the launched
	// jobs must carry, exactly as a direct /v1/jobs submission would. Without these the
	// intake path silently dropped the buyer's spend cap, verification policy, reputation
	// floor, private-pool routing, and quote binding.
	QuoteID       string             `json:"quote_id,omitempty"`
	MaxUSD        float64            `json:"max_usd,omitempty"`
	MinReputation float32            `json:"min_reputation,omitempty"`
	PrivatePool   bool               `json:"private_pool,omitempty"`
	Verification  VerificationPolicy `json:"verification,omitempty"`
}

// handleLaunchIntake takes a detected intake, fetches the source files, EXTRACTS
// the job input itself (the buyer formats nothing), and submits the pipeline's
// primary workload as a real job. Multi-stage chaining (a later stage on an
// earlier stage's output) is the workflow layer that plugs in next; this runs the
// primary stage and links the job to the intake. GitHub-gated: an unconfigured app
// returns an honest 503, never a fake job.
func (s *Server) handleLaunchIntake(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var req launchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid launch body")
		return
	}
	sourceID, ref, _, pipelineJSON, err := s.store.GetIntake(r.Context(), auth.BuyerID, req.IntakeID)
	if err != nil {
		writeErr(w, http.StatusNotFound, "intake not found")
		return
	}
	var det DetectedPipeline
	if err := json.Unmarshal(pipelineJSON, &det); err != nil || !det.Supported || len(det.Stages) == 0 {
		writeErr(w, http.StatusBadRequest, "intake has no runnable pipeline")
		return
	}
	// Refuse a recognized-but-not-launchable workload EARLY (item 20), before fetching
	// the source — e.g. audio transcription from a repo, whose binary-audio input path
	// is not wired. Keyed on the persisted pattern name so it holds for old intakes too.
	if !patternLaunchable(det.Pattern) {
		writeErr(w, http.StatusBadRequest, "this workload is recognized but not launchable from a repo yet (audio transcription needs the binary-audio upload path, not a repo fetch)")
		return
	}
	src, err := s.store.GetGitSource(r.Context(), auth.BuyerID, sourceID)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "intake has no connected source to fetch from")
		return
	}
	repo := req.Repo
	if repo == "" {
		repo = src.RepoFullName
	}
	if req.Ref != "" {
		ref = req.Ref
	}
	if ref == "" {
		ref = "HEAD"
	}
	files, _, err := githubApp.Tree(r.Context(), src.AccessToken, repo, ref)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	jsonl, stats, err := s.extractInput(r.Context(), src.AccessToken, repo, ref, det.Pattern, files)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "could not prepare input: "+err.Error())
		return
	}
	if stats.Records == 0 {
		writeErr(w, http.StatusBadRequest, "no input records extracted from the source")
		return
	}
	inputJSON, _ := json.Marshal(string(jsonl)) // a JSON string IS the inline JSONL
	intakeID, _ := uuid.Parse(req.IntakeID)
	// LaunchContract (items 1-2, 5): stamp the buyer's budget/verification/routing onto
	// every launched stage so an intake-launched job carries the SAME guarantees as a
	// direct /v1/jobs submission, instead of silently dropping the spend cap + routing.
	contract := LaunchContract{
		QuoteID:       req.QuoteID,
		MaxUSD:        req.MaxUSD,
		MinReputation: req.MinReputation,
		PrivatePool:   req.PrivatePool,
		Verification:  req.Verification,
	}
	if contract.MaxUSD <= 0 {
		// Item 2: an auto-launched intake must carry a BUDGET, never run uncapped. With no
		// buyer cap, GENERATE a composite quote for the detected stages (priced on the
		// extracted input) and use its worst-case total as the spend cap (max_usd), which
		// createJob then persists on every launched job. The launch is no longer a back
		// door around the budget governor.
		var stageQuotes []Quote
		for _, stage := range det.Stages {
			if stage.From == "previous" {
				continue
			}
			stageQuotes = append(stageQuotes, s.buildQuote(r.Context(), jobSubmit{
				JobType: JobType{Type: stage.Op},
				Model:   ModelRef{Kind: "gguf", Ref: stage.Model},
				Tier:    "batch",
				Input:   inputJSON,
			}, jsonl))
		}
		contract.MaxUSD = composeQuotes(stageQuotes).TotalCost.MaxUSD
	}
	var launched []map[string]any
	for i, stage := range det.Stages {
		if stage.From == "previous" {
			continue // chained: advanceIntake submits it when its predecessor completes
		}
		resp, herr := s.createJob(r.Context(), auth.BuyerID, contract.applyTo(jobSubmit{
			JobType: JobType{Type: stage.Op},
			Model:   ModelRef{Kind: "gguf", Ref: stage.Model},
			Tier:    "batch",
			Input:   inputJSON,
		}))
		if herr != nil {
			writeErr(w, herr.status, herr.msg)
			return
		}
		_ = s.store.InsertIntakeJobLink(r.Context(), resp.JobID, intakeID, i)
		if i == 0 {
			_ = s.store.UpdateIntakeJob(r.Context(), intakeID, resp.JobID)
		}
		launched = append(launched, map[string]any{"stage": stage.Op, "job_id": resp.JobID})
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"intake_id": req.IntakeID, "records": stats.Records, "extraction": stats, "jobs": launched})
}

// advanceIntake chains a multi-stage pipeline: when an intake-linked job completes,
// if the NEXT stage is declared From="previous", it submits that stage using the
// completed job's merged output as input. Best-effort + gated (acts only on intake
// jobs), so it can never break the lifecycle. Current patterns are fan-out (all
// stages run on the source input), so this is a no-op for them; it powers true
// output→input chains (e.g. transcribe → summarize) as those patterns land.
func (s *Server) advanceIntake(ctx context.Context, jobID uuid.UUID) {
	intakeID, stageIdx, ok := s.store.IntakeForJob(ctx, jobID)
	if !ok {
		return
	}
	pj, err := s.store.IntakePipeline(ctx, intakeID)
	if err != nil {
		return
	}
	var det DetectedPipeline
	if err := json.Unmarshal(pj, &det); err != nil {
		return
	}
	next := stageIdx + 1
	if next >= len(det.Stages) || det.Stages[next].From != "previous" {
		return
	}
	if s.store.IntakeStageSubmitted(ctx, intakeID, next) {
		return
	}
	ref, err := s.store.JobOutputRef(ctx, jobID)
	if err != nil || ref == "" {
		return
	}
	out, err := s.storage.GetObject(ctx, ref)
	if err != nil {
		return
	}
	buyerID, _, err := s.store.JobChargeInfo(ctx, jobID)
	if err != nil {
		return
	}
	stage := det.Stages[next]
	inputJSON, _ := json.Marshal(string(out))
	resp, herr := s.createJob(ctx, buyerID, jobSubmit{
		JobType: JobType{Type: stage.Op},
		Model:   ModelRef{Kind: "gguf", Ref: stage.Model},
		Tier:    "batch",
		Input:   inputJSON,
	})
	if herr != nil {
		log.Printf("intake %s: chaining to stage %d (%s) failed: %s", intakeID, next, stage.Op, herr.msg)
		return
	}
	_ = s.store.InsertIntakeJobLink(ctx, resp.JobID, intakeID, next)
}

// hasAnySuffix reports whether s (already lowercased) ends in any of suffixes.
func hasAnySuffix(s string, suffixes []string) bool {
	for _, suf := range suffixes {
		if strings.HasSuffix(s, suf) {
			return true
		}
	}
	return false
}

// fetchCappedDocs fetches each file whose path matches one of `exts`, enforcing a
// per-file cap and an aggregate cap so a malicious or accidental large repo cannot
// exhaust control-plane memory (items 16-17). Either overflow returns the typed
// errIntakeTooLarge. Pure over `fetch`, so the caps are unit-tested with a fake
// fetcher (no GitHub). Detection and extraction share `exts` (documentSetExts), so a
// matched file is always actually fetched (no "supported then 0 records", item 19).
func fetchCappedDocs(files []RepoFile, exts []string, fetch func(path string) ([]byte, error), perFile, aggregate int64) ([]namedContent, error) {
	var docs []namedContent
	var total int64
	for _, f := range files {
		if !hasAnySuffix(strings.ToLower(f.Path), exts) {
			continue
		}
		content, err := fetch(f.Path)
		if err != nil {
			return nil, err
		}
		if int64(len(content)) > perFile {
			return nil, fmt.Errorf("%w: %s exceeds %d bytes", errIntakeTooLarge, f.Path, perFile)
		}
		total += int64(len(content))
		if total > aggregate {
			return nil, fmt.Errorf("%w: document set exceeds %d bytes aggregate", errIntakeTooLarge, aggregate)
		}
		docs = append(docs, namedContent{Path: f.Path, Content: content})
	}
	return docs, nil
}

// extractInput fetches + extracts the JSONL the detected pattern needs. The
// extractors are pure (extract.go); this is the I/O that feeds them.
func (s *Server) extractInput(ctx context.Context, token, repo, ref, pat string, files []RepoFile) ([]byte, ExtractionStats, error) {
	switch pat {
	case "tabular-text":
		for _, f := range files {
			if !hasAnySuffix(strings.ToLower(f.Path), tabularExts) {
				continue
			}
			content, err := githubApp.RawFile(ctx, token, repo, ref, f.Path)
			if err != nil {
				return nil, ExtractionStats{}, err
			}
			out, n, err := extractTabular(f.Path, content)
			if err != nil {
				return nil, ExtractionStats{}, err
			}
			used := []namedContent{{Path: f.Path, Content: content}}
			return out, docStats(files, tabularExts, used, n), nil
		}
		return nil, ExtractionStats{}, fmt.Errorf("no tabular file found in source")
	case "document-set":
		docs, err := fetchCappedDocs(files, documentSetExts, func(p string) ([]byte, error) {
			return githubApp.RawFile(ctx, token, repo, ref, p)
		}, maxIntakeFileBytes, maxIntakeTotalBytes)
		if err != nil {
			return nil, ExtractionStats{}, err
		}
		out, n := extractDocuments(docs)
		return out, docStats(files, documentSetExts, docs, n), nil
	case "code-repo":
		docs, err := fetchCappedDocs(files, codeRepoExts, func(p string) ([]byte, error) {
			return githubApp.RawFile(ctx, token, repo, ref, p)
		}, maxIntakeFileBytes, maxIntakeTotalBytes)
		if err != nil {
			return nil, ExtractionStats{}, err
		}
		out, n := extractCode(docs, codeChunkLines)
		return out, docStats(files, codeRepoExts, docs, n), nil
	case "audio-transcribe":
		return nil, ExtractionStats{}, fmt.Errorf("audio transcription input is binary audio — the audio upload path is the next wiring step (not faked)")
	}
	return nil, ExtractionStats{}, fmt.Errorf("pattern %q has no input extractor yet", pat)
}

// --- result writeback: a reviewable PR, never a silent edit ---

// ghJSON does a JSON GitHub API call and decodes the object body.
func (g *GitHubApp) ghJSON(ctx context.Context, method, token, path string, body any) (map[string]any, error) {
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequestWithContext(ctx, method, "https://api.github.com/"+path, rdr)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("github %s %s (%d): %s", method, path, resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out, nil
}

func (g *GitHubApp) refSHA(ctx context.Context, token, repo, branch string) (string, error) {
	out, err := g.ghJSON(ctx, http.MethodGet, token, fmt.Sprintf("repos/%s/git/ref/heads/%s", repo, branch), nil)
	if err != nil {
		return "", err
	}
	obj, _ := out["object"].(map[string]any)
	sha, _ := obj["sha"].(string)
	if sha == "" {
		return "", fmt.Errorf("no sha for %s@%s", repo, branch)
	}
	return sha, nil
}

// deliverPR opens a pull request that adds `path` with `content` on a new branch —
// the safe writeback path (a reviewable PR, never a silent in-place edit). Real +
// gated; honest error without a token/app.
func (g *GitHubApp) deliverPR(ctx context.Context, token, repo, base, branch, path string, content []byte, title string) (string, error) {
	if !g.Configured() {
		return "", errGitHubUnconfigured
	}
	baseSHA, err := g.refSHA(ctx, token, repo, base)
	if err != nil {
		return "", err
	}
	if _, err := g.ghJSON(ctx, http.MethodPost, token, fmt.Sprintf("repos/%s/git/refs", repo), map[string]any{"ref": "refs/heads/" + branch, "sha": baseSHA}); err != nil {
		return "", err
	}
	if _, err := g.ghJSON(ctx, http.MethodPut, token, fmt.Sprintf("repos/%s/contents/%s", repo, path), map[string]any{
		"message": title, "content": base64.StdEncoding.EncodeToString(content), "branch": branch,
	}); err != nil {
		return "", err
	}
	pr, err := g.ghJSON(ctx, http.MethodPost, token, fmt.Sprintf("repos/%s/pulls", repo), map[string]any{"title": title, "head": branch, "base": base})
	if err != nil {
		return "", err
	}
	urlStr, _ := pr["html_url"].(string)
	return urlStr, nil
}

type deliverRequest struct {
	SourceID string `json:"source_id"`
	Repo     string `json:"repo"`
	Base     string `json:"base"`
	Branch   string `json:"branch"`
	Path     string `json:"path"`
	Content  string `json:"content"`
	Title    string `json:"title"`
}

// handleDeliver opens a PR on the buyer's connected repo with the provided result
// content (the backend for the result-delivery "Open PR" action). Gated on a
// configured GitHub app + token.
func (s *Server) handleDeliver(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var req deliverRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid deliver body")
		return
	}
	src, err := s.store.GetGitSource(r.Context(), auth.BuyerID, req.SourceID)
	if err != nil {
		writeErr(w, http.StatusNotFound, "source not found")
		return
	}
	repo := req.Repo
	if repo == "" {
		repo = src.RepoFullName
	}
	base := req.Base
	if base == "" {
		if base = src.DefaultBranch; base == "" {
			base = "main"
		}
	}
	branch := req.Branch
	if branch == "" {
		branch = "cx-results"
	}
	if req.Path == "" {
		req.Path = "cx/results.jsonl"
	}
	title := req.Title
	if title == "" {
		title = "Computexchange results"
	}
	prURL, err := githubApp.deliverPR(r.Context(), src.AccessToken, repo, base, branch, req.Path, []byte(req.Content), title)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"pull_request": prURL})
}
