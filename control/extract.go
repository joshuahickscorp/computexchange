package main

import (
	"bufio"
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"strings"
)

// extract.go — turn a connected source's raw files into a job's JSONL input, per
// detected pattern. This is the "reduce user input to zero" layer: the buyer never
// formats data — the system reads their CSV/docs and produces the {"text":...}
// lines the workload runs on. The parsers are PURE (bytes → bytes), so they are
// fully unit-tested without GitHub.

// namedContent is one fetched file: its path + raw bytes.
type namedContent struct {
	Path    string
	Content []byte
}

// jsonlLine encodes one {"text": ...} record (the embed/classify/extract shape).
func jsonlLine(text string) []byte {
	b, _ := json.Marshal(map[string]string{"text": text})
	return append(b, '\n')
}

// extractTabular turns a CSV/TSV into JSONL by picking the most text-heavy column
// (the obvious content column, so the buyer never names it) and emitting one record
// per row. A .jsonl input is already line-shaped and passes through (each line
// validated). Returns the JSONL bytes and the record count.
func extractTabular(name string, content []byte) ([]byte, int, error) {
	lower := strings.ToLower(name)
	if strings.HasSuffix(lower, ".jsonl") {
		var out bytes.Buffer
		n := 0
		sc := bufio.NewScanner(bytes.NewReader(content))
		sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for sc.Scan() {
			line := bytes.TrimSpace(sc.Bytes())
			if len(line) == 0 {
				continue
			}
			if !json.Valid(line) {
				return nil, 0, fmt.Errorf("jsonl line %d is not valid JSON", n+1)
			}
			out.Write(line)
			out.WriteByte('\n')
			n++
		}
		return out.Bytes(), n, sc.Err()
	}
	r := csv.NewReader(bytes.NewReader(content))
	if strings.HasSuffix(lower, ".tsv") {
		r.Comma = '\t'
	}
	r.FieldsPerRecord = -1
	rows, err := r.ReadAll()
	if err != nil {
		return nil, 0, fmt.Errorf("parsing %s: %w", name, err)
	}
	if len(rows) == 0 {
		return nil, 0, fmt.Errorf("%s is empty", name)
	}
	col := textColumn(rows)
	var out bytes.Buffer
	n := 0
	for _, row := range rows[1:] {
		if col >= len(row) {
			continue
		}
		text := strings.TrimSpace(row[col])
		if text == "" {
			continue
		}
		out.Write(jsonlLine(text))
		n++
	}
	return out.Bytes(), n, nil
}

// textColumn picks the column with the highest average text length across sampled
// data rows — the "content" column a buyer means, without making them name it.
func textColumn(rows [][]string) int {
	if len(rows) < 2 {
		return 0
	}
	cols := len(rows[0])
	best, bestAvg := 0, -1.0
	sample := rows[1:]
	if len(sample) > 50 {
		sample = sample[:50]
	}
	for c := 0; c < cols; c++ {
		total, count := 0, 0
		for _, row := range sample {
			if c < len(row) {
				total += len(strings.TrimSpace(row[c]))
				count++
			}
		}
		if count == 0 {
			continue
		}
		if avg := float64(total) / float64(count); avg > bestAvg {
			bestAvg, best = avg, c
		}
	}
	return best
}

// extractDocuments turns a set of text documents into JSONL, one record per doc.
func extractDocuments(files []namedContent) ([]byte, int) {
	var out bytes.Buffer
	n := 0
	for _, f := range files {
		text := strings.TrimSpace(string(f.Content))
		if text == "" {
			continue
		}
		out.Write(jsonlLine(text))
		n++
	}
	return out.Bytes(), n
}

// codeChunkLines is the deterministic chunk size for code-repo embedding (item 21):
// each source file is split into fixed-size line windows so a repo maps to a stable,
// reproducible set of embed records. Same input bytes -> same chunks -> same embeddings,
// which the redundancy verifier requires (identical chunking across workers).
const codeChunkLines = 50

// extractCode turns fetched source files into a CHUNKED embed JSONL: each file is split
// into windows of `linesPerChunk` lines, one {"text":...} record per non-empty chunk.
// PURE and deterministic — stable file order (from the caller), a fixed window, and
// preserved line order — so the same repo always yields the same embed inputs (item 21).
func extractCode(files []namedContent, linesPerChunk int) ([]byte, int) {
	if linesPerChunk < 1 {
		linesPerChunk = 1
	}
	var out bytes.Buffer
	n := 0
	for _, f := range files {
		lines := strings.Split(string(f.Content), "\n")
		for i := 0; i < len(lines); i += linesPerChunk {
			end := i + linesPerChunk
			if end > len(lines) {
				end = len(lines)
			}
			chunk := strings.TrimSpace(strings.Join(lines[i:end], "\n"))
			if chunk == "" {
				continue
			}
			out.Write(jsonlLine(chunk))
			n++
		}
	}
	return out.Bytes(), n
}
