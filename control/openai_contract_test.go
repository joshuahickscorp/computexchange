package main

import (
	"bytes"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestSpoolBoundedOpenAIUploadRejectsInsteadOfTruncating(t *testing.T) {
	const limit = int64(4)

	atLimit, size, err := spoolBoundedOpenAIUpload(strings.NewReader("1234"), limit)
	if err != nil {
		t.Fatalf("spool at limit: %v", err)
	}
	name := atLimit.Name()
	defer func() {
		_ = atLimit.Close()
		_ = os.Remove(name)
	}()
	if size != limit {
		t.Fatalf("spooled size = %d, want %d", size, limit)
	}
	got, err := io.ReadAll(atLimit)
	if err != nil {
		t.Fatalf("read staged body: %v", err)
	}
	if string(got) != "1234" {
		t.Fatalf("staged body = %q, want exact body", got)
	}

	oversized, _, err := spoolBoundedOpenAIUpload(strings.NewReader("12345"), limit)
	if !errors.Is(err, errOpenAIUploadTooLarge) {
		t.Fatalf("oversized spool error = %v, want %v", err, errOpenAIUploadTooLarge)
	}
	if oversized != nil {
		t.Fatal("oversized spool retained a truncated temporary file")
	}

	if _, _, err := spoolBoundedOpenAIUpload(strings.NewReader("x"), 0); err == nil {
		t.Fatal("non-positive upload limit was accepted")
	}
}

func TestSpoolBoundedOpenAIUploadPropagatesReaderFailure(t *testing.T) {
	want := errors.New("read failed")
	if _, _, err := spoolBoundedOpenAIUpload(errorReader{err: want}, 4); !errors.Is(err, want) {
		t.Fatalf("reader error = %v, want %v", err, want)
	}
}

func TestOpenAIUploadContentCheckStreamsWhitespace(t *testing.T) {
	for _, tc := range []struct {
		body string
		want bool
	}{
		{body: " \t\r\n\u2003", want: false},
		{body: " \n{\"custom_id\":\"one\"}\n", want: true},
	} {
		got, err := openAIUploadHasContent(strings.NewReader(tc.body))
		if err != nil {
			t.Fatalf("content check for %q: %v", tc.body, err)
		}
		if got != tc.want {
			t.Fatalf("content check for %q = %v, want %v", tc.body, got, tc.want)
		}
	}
}

func TestStageOpenAIFileUploadStreamsStrictMultipartToDisk(t *testing.T) {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("purpose", "batch"); err != nil {
		t.Fatalf("write purpose: %v", err)
	}
	part, err := writer.CreateFormFile("file", "requests.jsonl")
	if err != nil {
		t.Fatalf("create file part: %v", err)
	}
	want := []byte("{\"custom_id\":\"one\"}\n")
	if _, err := part.Write(want); err != nil {
		t.Fatalf("write file part: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/files", &body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	upload, httpErr := stageOpenAIFileUpload(req)
	if httpErr != nil {
		t.Fatalf("stage multipart: %v", httpErr)
	}
	name := upload.file.Name()
	if upload.size != int64(len(want)) || upload.filename != "requests.jsonl" || upload.purpose != "batch" {
		t.Fatalf("staged metadata = size %d filename %q purpose %q", upload.size, upload.filename, upload.purpose)
	}
	info, err := upload.file.Stat()
	if err != nil {
		t.Fatalf("stat staged file: %v", err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("staged file mode = %o, want 600", got)
	}
	if _, err := upload.file.Seek(0, io.SeekStart); err != nil {
		t.Fatalf("rewind staged file: %v", err)
	}
	got, err := io.ReadAll(upload.file)
	if err != nil {
		t.Fatalf("read staged file: %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("staged file = %q, want %q", got, want)
	}
	upload.close()
	if _, err := os.Stat(name); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("temporary upload remained after close: %v", err)
	}
}

func TestStageOpenAIFileUploadRejectsUnknownAndWhitespaceOnlyMultipart(t *testing.T) {
	for _, tc := range []struct {
		name      string
		fieldName string
		contents  string
		status    int
	}{
		{name: "unknown field", fieldName: "extra", contents: "{}\n", status: http.StatusBadRequest},
		{name: "whitespace only", fieldName: "", contents: " \t\r\n", status: http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var body bytes.Buffer
			writer := multipart.NewWriter(&body)
			if tc.fieldName != "" {
				if err := writer.WriteField(tc.fieldName, "nope"); err != nil {
					t.Fatalf("write extra field: %v", err)
				}
			}
			part, err := writer.CreateFormFile("file", "requests.jsonl")
			if err != nil {
				t.Fatalf("create file part: %v", err)
			}
			if _, err := part.Write([]byte(tc.contents)); err != nil {
				t.Fatalf("write file part: %v", err)
			}
			if err := writer.Close(); err != nil {
				t.Fatalf("close multipart: %v", err)
			}
			req := httptest.NewRequest(http.MethodPost, "/v1/files", &body)
			req.Header.Set("Content-Type", writer.FormDataContentType())
			if upload, httpErr := stageOpenAIFileUpload(req); httpErr == nil || httpErr.status != tc.status {
				if upload != nil {
					upload.close()
				}
				t.Fatalf("stage result = upload %v error %v, want HTTP %d", upload != nil, httpErr, tc.status)
			}
		})
	}
}

type errorReader struct{ err error }

func (r errorReader) Read([]byte) (int, error) { return 0, r.err }

var _ io.Reader = errorReader{}

func TestOpenAIBatchModelNamesAcceptOnlyNativeIdentityOrOmittedDefault(t *testing.T) {
	tests := []struct {
		name     string
		jobType  string
		input    string
		expected string
	}{
		{name: "embed omitted", jobType: "embed", input: "", expected: "all-minilm-l6-v2"},
		{name: "embed native", jobType: "embed", input: "all-minilm-l6-v2", expected: "all-minilm-l6-v2"},
		{name: "chat omitted", jobType: "batch_infer", input: "", expected: "llama-3.2-1b-instruct-q4"},
		{name: "chat native", jobType: "batch_infer", input: "llama-3.2-1b-instruct-q4", expected: "llama-3.2-1b-instruct-q4"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := mapModel(tc.jobType, tc.input)
			if err != nil {
				t.Fatalf("mapModel(%q, %q): %v", tc.jobType, tc.input, err)
			}
			if got != tc.expected {
				t.Fatalf("mapModel(%q, %q) = %q, want %q", tc.jobType, tc.input, got, tc.expected)
			}
		})
	}
}

func TestOpenAIBatchRejectsBrandedCrossModelAliasesWithNativeHint(t *testing.T) {
	tests := []struct {
		jobType  string
		model    string
		endpoint string
		native   string
	}{
		{jobType: "embed", model: "text-embedding-3-small", endpoint: "/v1/embeddings", native: "all-minilm-l6-v2"},
		{jobType: "embed", model: "text-embedding-3-large", endpoint: "/v1/embeddings", native: "all-minilm-l6-v2"},
		{jobType: "embed", model: "text-embedding-ada-002", endpoint: "/v1/embeddings", native: "all-minilm-l6-v2"},
		{jobType: "batch_infer", model: "gpt-4o-mini", endpoint: "/v1/chat/completions", native: "llama-3.2-1b-instruct-q4"},
		{jobType: "batch_infer", model: "gpt-4o", endpoint: "/v1/chat/completions", native: "llama-3.2-1b-instruct-q4"},
		{jobType: "batch_infer", model: "gpt-4", endpoint: "/v1/chat/completions", native: "llama-3.2-1b-instruct-q4"},
		{jobType: "batch_infer", model: "gpt-4-turbo", endpoint: "/v1/chat/completions", native: "llama-3.2-1b-instruct-q4"},
		{jobType: "batch_infer", model: "gpt-3.5-turbo", endpoint: "/v1/chat/completions", native: "llama-3.2-1b-instruct-q4"},
	}
	for _, tc := range tests {
		t.Run(tc.model, func(t *testing.T) {
			got, err := mapModel(tc.jobType, tc.model)
			if got != "" {
				t.Fatalf("mapModel(%q, %q) returned misleading target %q", tc.jobType, tc.model, got)
			}
			var notFound *modelNotFoundError
			if !errors.As(err, &notFound) {
				t.Fatalf("mapModel(%q, %q) error = %T %v, want *modelNotFoundError", tc.jobType, tc.model, err, err)
			}
			if notFound.endpoint != tc.endpoint || notFound.nativeModelHint != tc.native {
				t.Fatalf("rejection = %+v, want endpoint %q and native hint %q", notFound, tc.endpoint, tc.native)
			}
			message := err.Error()
			for _, fragment := range []string{tc.model, tc.endpoint, `use native model "` + tc.native + `"`} {
				if !strings.Contains(message, fragment) {
					t.Fatalf("actionable rejection %q missing %q", message, fragment)
				}
			}
		})
	}
}
