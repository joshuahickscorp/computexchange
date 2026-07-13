package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime"
	"mime/multipart"
	"net/http"
	"strconv"
	"strings"
)

const (
	audioUploadJobType      = "audio_transcribe"
	audioUploadDefaultModel = "whisper-tiny"
	audioUploadBodyOverhead = 64 << 10
	audioUploadMaxScalar    = 2048
)

const audioUploadBoundaryError = "audio_transcribe requires a validated WAV upload through POST /v1/audio/jobs or POST /v1/audio/jobs/quote"

// AudioUploadMetadata is the buyer-visible statement of the raw WAV facts used
// for pricing. The ordinary quote input byte count and the job's economic input
// byte count continue to describe the normalized JSONL actually dispatched to a
// worker; RawWAVBytes and SHA256 below describe the uploaded container instead.
type AudioUploadMetadata struct {
	Samples                int64   `json:"samples"`
	SampleRateHz           int     `json:"sample_rate_hz"`
	DurationSeconds        float64 `json:"duration_seconds"`
	DurationMinutes        float64 `json:"duration_minutes"`
	RawWAVBytes            int64   `json:"raw_wav_bytes"`
	SHA256                 string  `json:"sha256"`
	PricePerAudioMinuteUSD float64 `json:"price_per_audio_minute_usd"`
}

type audioAdmission struct {
	facts                 audioUploadFacts
	normalizedJSONLSHA256 [sha256.Size]byte
	pricePerAudioMinute   float64
}

func (f audioUploadFacts) publicMetadata() *AudioUploadMetadata {
	return &AudioUploadMetadata{
		Samples:         f.samples,
		SampleRateHz:    audioUploadSampleRate,
		DurationSeconds: float64(f.samples) / float64(audioUploadSampleRate),
		DurationMinutes: f.durationMinutes,
		RawWAVBytes:     f.rawBytes,
		SHA256:          f.sha256,
	}
}

func audioUploadMetadata(admission *audioAdmission) *AudioUploadMetadata {
	if admission == nil {
		return nil
	}
	meta := admission.facts.publicMetadata()
	meta.PricePerAudioMinuteUSD = admission.pricePerAudioMinute
	return meta
}

type audioMultipartFields struct {
	rawWAV  []byte
	model   string
	tier    string
	quoteID string
	maxUSD  float64
}

// parseAudioMultipart is the only HTTP wire decoder for audio. It uses
// NextRawPart so transfer encodings are never silently transformed, accepts one
// file and a closed scalar allowlist, and applies a route-local cap only 64 KiB
// above the maximum accepted WAV.
func parseAudioMultipart(w http.ResponseWriter, r *http.Request, submit bool) (audioMultipartFields, *httpError) {
	var out audioMultipartFields
	r.Body = http.MaxBytesReader(w, r.Body, audioUploadMaxRawBytes+audioUploadBodyOverhead)

	mediaType, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || mediaType != "multipart/form-data" || params["boundary"] == "" {
		return out, &httpError{http.StatusUnsupportedMediaType, "Content-Type must be multipart/form-data with a boundary"}
	}
	for key := range params {
		if key != "boundary" {
			return out, &httpError{http.StatusBadRequest, "multipart Content-Type contains an unsupported parameter: " + key}
		}
	}

	mr := multipart.NewReader(r.Body, params["boundary"])
	seen := make(map[string]bool)
	for {
		part, nextErr := mr.NextRawPart()
		if errors.Is(nextErr, io.EOF) {
			break
		}
		if nextErr != nil {
			var maxErr *http.MaxBytesError
			if errors.As(nextErr, &maxErr) {
				return out, &httpError{http.StatusRequestEntityTooLarge, "audio multipart body exceeds its bounded upload limit"}
			}
			return out, &httpError{http.StatusBadRequest, "invalid audio multipart body: " + nextErr.Error()}
		}

		name := part.FormName()
		if name == "" {
			part.Close()
			return out, &httpError{http.StatusBadRequest, "every multipart part must have a form field name"}
		}
		if seen[name] {
			part.Close()
			return out, &httpError{http.StatusBadRequest, "duplicate multipart field: " + name}
		}
		seen[name] = true

		switch name {
		case "file":
			if part.FileName() == "" {
				part.Close()
				return out, &httpError{http.StatusBadRequest, "file must be a multipart file upload"}
			}
			raw, readErr := readBoundedAudioPart(part, audioUploadMaxRawBytes)
			part.Close()
			if errors.Is(readErr, errAudioPartTooLarge) {
				return out, &httpError{http.StatusRequestEntityTooLarge, fmt.Sprintf("audio WAV exceeds the %d-byte limit", audioUploadMaxRawBytes)}
			}
			if readErr != nil {
				var maxErr *http.MaxBytesError
				if errors.As(readErr, &maxErr) {
					return out, &httpError{http.StatusRequestEntityTooLarge, "audio multipart body exceeds its bounded upload limit"}
				}
				return out, &httpError{http.StatusBadRequest, "reading audio WAV: " + readErr.Error()}
			}
			out.rawWAV = raw

		case "model", "tier", "quote_id", "max_usd":
			if part.FileName() != "" {
				part.Close()
				return out, &httpError{http.StatusBadRequest, name + " must be a scalar multipart field"}
			}
			if !submit && (name == "quote_id" || name == "max_usd") {
				part.Close()
				return out, &httpError{http.StatusBadRequest, "unsupported multipart field for an audio quote: " + name}
			}
			value, readErr := readBoundedAudioScalar(part)
			part.Close()
			if readErr != nil {
				return out, &httpError{http.StatusBadRequest, readErr.Error()}
			}
			switch name {
			case "model":
				out.model = value
			case "tier":
				out.tier = value
			case "quote_id":
				out.quoteID = value
			case "max_usd":
				v, parseErr := strconv.ParseFloat(value, 64)
				if parseErr != nil || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
					return out, &httpError{http.StatusBadRequest, "max_usd must be a finite non-negative number"}
				}
				out.maxUSD = v
			}

		default:
			part.Close()
			return out, &httpError{http.StatusBadRequest, "unsupported multipart field: " + name}
		}
	}

	if !seen["file"] {
		return out, &httpError{http.StatusBadRequest, "exactly one file WAV is required"}
	}
	if out.model == "" {
		out.model = audioUploadDefaultModel
	}
	if out.tier == "" {
		out.tier = "batch"
	}
	return out, nil
}

var errAudioPartTooLarge = errors.New("audio multipart part exceeds limit")

func readBoundedAudioPart(r io.Reader, max int) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, int64(max)+1))
	if err != nil {
		return nil, err
	}
	if len(b) > max {
		return nil, errAudioPartTooLarge
	}
	return b, nil
}

func readBoundedAudioScalar(r io.Reader) (string, error) {
	b, err := io.ReadAll(io.LimitReader(r, audioUploadMaxScalar+1))
	if err != nil {
		return "", err
	}
	if len(b) > audioUploadMaxScalar {
		return "", errors.New("audio multipart scalar exceeds its 2048-byte limit")
	}
	value := strings.TrimSpace(string(b))
	if value == "" {
		return "", errors.New("audio multipart scalar fields must be nonempty")
	}
	return value, nil
}

func audioJobSubmission(fields audioMultipartFields) (jobSubmit, *httpError) {
	jsonl, facts, err := normalizeAudioUploadWAV(fields.rawWAV)
	if err != nil {
		return jobSubmit{}, &httpError{http.StatusBadRequest, err.Error()}
	}
	input, err := json.Marshal(string(jsonl))
	if err != nil {
		return jobSubmit{}, &httpError{http.StatusInternalServerError, "encoding normalized audio input: " + err.Error()}
	}
	params := json.RawMessage(`{"split_size":1}`)
	admission := &audioAdmission{
		facts:                 facts,
		normalizedJSONLSHA256: sha256.Sum256(jsonl),
	}
	return jobSubmit{
		JobType: JobType{Type: audioUploadJobType},
		Model:   ModelRef{Kind: "hf", Ref: fields.model},
		Params:  params,
		Verification: VerificationPolicy{
			RedundancyFrac: 1,
		},
		Tier:           fields.tier,
		Input:          input,
		MaxUSD:         fields.maxUSD,
		QuoteID:        fields.quoteID,
		audioAdmission: admission,
	}, nil
}

func rejectUntrustedAudioSubmission(sub jobSubmit) *httpError {
	if sub.JobType.Type == audioUploadJobType && sub.audioAdmission == nil {
		return &httpError{http.StatusBadRequest, audioUploadBoundaryError}
	}
	return nil
}

// prepareAudioPricing replaces every buyer-controlled pricing proxy with the
// catalogue's USD/audio-minute price and the duration derived from the WAV data
// chunk. It is called before quote construction or any createJob storage write.
func (s *Server) prepareAudioPricing(ctx context.Context, sub *jobSubmit) *httpError {
	if sub.JobType.Type != audioUploadJobType {
		return nil
	}
	if sub.audioAdmission == nil {
		return &httpError{http.StatusBadRequest, audioUploadBoundaryError}
	}
	m, err := s.store.GetModel(ctx, sub.Model.Ref)
	if err != nil {
		return &httpError{http.StatusServiceUnavailable, "audio model pricing is unavailable: " + err.Error()}
	}
	if m.JobType != audioUploadJobType || m.Kind != "whisper" || m.PricePerUnit <= 0 ||
		math.IsNaN(m.PricePerUnit) || math.IsInf(m.PricePerUnit, 0) {
		return &httpError{http.StatusServiceUnavailable, "audio model has no valid Whisper USD/audio-minute catalogue row"}
	}
	sub.audioAdmission.pricePerAudioMinute = m.PricePerUnit
	if estimateAudioUploadUSD(sub.audioAdmission.facts, sub.audioAdmission.pricePerAudioMinute, sub.Tier) <= 0 {
		return &httpError{http.StatusBadRequest, "audio clip is too short to produce a non-zero billable estimate"}
	}
	return nil
}

func estimateAudioUploadUSD(facts audioUploadFacts, pricePerMinute float64, tier string) float64 {
	if facts.durationMinutes <= 0 || pricePerMinute <= 0 || math.IsNaN(pricePerMinute) || math.IsInf(pricePerMinute, 0) {
		return 0
	}
	return roundUSD(facts.durationMinutes * pricePerMinute * tierMultiplier(tier))
}

// audioOfferedRateUSDHr converts the existing representative audio throughput
// (clips/second) into its matching source-audio minutes/hour using this clip's
// admitted duration. This avoids treating a per-minute catalogue price as a
// token price or assuming every accepted clip has the same duration.
func audioOfferedRateUSDHr(facts audioUploadFacts, pricePerMinute, clipsPerSecond float64) float32 {
	if facts.durationMinutes <= 0 || pricePerMinute <= 0 || clipsPerSecond <= 0 ||
		math.IsNaN(pricePerMinute) || math.IsInf(pricePerMinute, 0) {
		return 0
	}
	return float32(pricePerMinute * facts.durationMinutes * clipsPerSecond * 3600)
}

func audioInitialEconomicPlan(sub jobSubmit, schedule EconomicSchedule) EconomicPlan {
	basePrimary := estimateAudioUploadUSD(sub.audioAdmission.facts, sub.audioAdmission.pricePerAudioMinute, sub.Tier)
	return BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD:   roundEconomicUSD(basePrimary * 2), // one primary + fixed 1.0 redundancy
		InitialTaskCount: 2,
		ExtraTaskReserve: economicExtraTaskReserve(1),
		SupplierShare:    supplierShareRate,
	}, schedule)
}

func (s *Server) estimateSubmissionUSD(ctx context.Context, sub jobSubmit, inputBytesLen, nLines int) float64 {
	if sub.JobType.Type == audioUploadJobType {
		if sub.audioAdmission == nil {
			return 0
		}
		return estimateAudioUploadUSD(sub.audioAdmission.facts, sub.audioAdmission.pricePerAudioMinute, sub.Tier)
	}
	return s.estimateJobUSD(ctx, sub.JobType.Type, sub.Model.Ref, inputBytesLen, nLines, sub.JobType.MaxTokens, sub.Tier)
}

func audioJobEventDetail(admission *audioAdmission) []byte {
	if admission == nil {
		return nil
	}
	// This detail makes the pricing inputs inspectable on the existing event
	// stream, but InsertJobEvent is deliberately best-effort. It is not a
	// substitute for a future durable economic_input_authority job column.
	b, _ := json.Marshal(map[string]any{"audio_input": audioUploadMetadata(admission)})
	return b
}

// validateAudioAdmissionInput binds the private duration/price facts to the
// exact normalized JSONL bytes being quoted or submitted. It prevents an
// internal caller from pairing clip A's short duration with clip B's payload.
func validateAudioAdmissionInput(sub jobSubmit, normalized []byte) error {
	if sub.JobType.Type != audioUploadJobType {
		return nil
	}
	if sub.audioAdmission == nil {
		return errors.New(audioUploadBoundaryError)
	}
	if sub.audioAdmission.pricePerAudioMinute <= 0 ||
		math.IsNaN(sub.audioAdmission.pricePerAudioMinute) ||
		math.IsInf(sub.audioAdmission.pricePerAudioMinute, 0) ||
		estimateAudioUploadUSD(sub.audioAdmission.facts, sub.audioAdmission.pricePerAudioMinute, sub.Tier) <= 0 {
		return errors.New("audio admission has no valid non-zero catalogue duration price")
	}
	scan := scanJSONL(normalized)
	if scan.Records != 1 || scan.MalformedRecords != 0 {
		return errors.New("audio admission requires exactly one valid normalized JSONL record")
	}
	if sha256.Sum256(normalized) != sub.audioAdmission.normalizedJSONLSHA256 {
		return errors.New("normalized audio input does not match its server-derived admission facts")
	}
	return nil
}

func (s *Server) handleAudioQuote(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	fields, herr := parseAudioMultipart(w, r, false)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	sub, herr := audioJobSubmission(fields)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	if !validTiers[sub.Tier] {
		writeErr(w, http.StatusBadRequest, "invalid tier: "+sub.Tier)
		return
	}
	if err := validateAdvertisedRuntimeJobModel(sub.JobType.Type, sub.Model.Ref); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if herr := s.prepareAudioPricing(r.Context(), &sub); herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	schedule, err := LoadEconomicScheduleFromEnv()
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, "economic schedule unavailable: "+err.Error())
		return
	}
	var normalizedInput string
	if err := json.Unmarshal(sub.Input, &normalizedInput); err != nil {
		// sub.Input was encoded immediately above from server-generated bytes.
		writeErr(w, http.StatusInternalServerError, "decoding normalized audio input")
		return
	}
	inputBytes := []byte(normalizedInput)
	if err := validateAudioAdmissionInput(sub, inputBytes); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	q := s.buildQuoteWithSchedule(r.Context(), auth.BuyerID, sub, inputBytes, schedule)
	if !q.Economics.Executable {
		writeErr(w, http.StatusConflict, "quote is not executable: "+q.Economics.BlockReason)
		return
	}
	if err := s.store.InsertQuote(r.Context(), auth.BuyerID, q); err != nil {
		writeErr(w, http.StatusInternalServerError, "persisting quote: "+err.Error())
		return
	}
	metrics.quotes.Add(1)
	writeJSON(w, http.StatusOK, q)
}

func (s *Server) handleAudioCreateJob(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	fields, herr := parseAudioMultipart(w, r, true)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	sub, herr := audioJobSubmission(fields)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	resp, herr := s.createJob(r.Context(), auth.BuyerID, sub)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	writeJSON(w, http.StatusAccepted, resp)
}
