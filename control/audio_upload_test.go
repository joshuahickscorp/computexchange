package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"
	"time"
)

type audioUploadTestChunk struct {
	id      string
	payload []byte
	pad     byte
}

func audioUploadTestFormat() []byte {
	format := make([]byte, 16)
	binary.LittleEndian.PutUint16(format[0:2], 1)
	binary.LittleEndian.PutUint16(format[2:4], 1)
	binary.LittleEndian.PutUint32(format[4:8], audioUploadSampleRate)
	binary.LittleEndian.PutUint32(format[8:12], audioUploadSampleRate*audioUploadBlockAlign)
	binary.LittleEndian.PutUint16(format[12:14], audioUploadBlockAlign)
	binary.LittleEndian.PutUint16(format[14:16], 16)
	return format
}

func audioUploadTestWAV(chunks ...audioUploadTestChunk) []byte {
	var body bytes.Buffer
	body.WriteString("WAVE")
	for _, chunk := range chunks {
		if len(chunk.id) != 4 {
			panic("test chunk id must have four bytes")
		}
		body.WriteString(chunk.id)
		var size [4]byte
		binary.LittleEndian.PutUint32(size[:], uint32(len(chunk.payload)))
		body.Write(size[:])
		body.Write(chunk.payload)
		if len(chunk.payload)%2 != 0 {
			body.WriteByte(chunk.pad)
		}
	}

	out := make([]byte, 8, 8+body.Len())
	copy(out[0:4], "RIFF")
	binary.LittleEndian.PutUint32(out[4:8], uint32(body.Len()))
	return append(out, body.Bytes()...)
}

func validAudioUploadTestWAV(samples int) []byte {
	return audioUploadTestWAV(
		audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
		audioUploadTestChunk{id: "data", payload: make([]byte, samples*audioUploadBlockAlign)},
	)
}

func TestNormalizeAudioUploadWAV(t *testing.T) {
	raw := validAudioUploadTestWAV(16000)
	jsonl, facts, err := normalizeAudioUploadWAV(raw)
	if err != nil {
		t.Fatalf("normalizeAudioUploadWAV: %v", err)
	}
	wantJSONL := []byte(fmt.Sprintf("{\"audio_b64\":%q}\n", base64.StdEncoding.EncodeToString(raw)))
	if !bytes.Equal(jsonl, wantJSONL) {
		t.Fatalf("normalized JSONL mismatch\n got: %s\nwant: %s", jsonl, wantJSONL)
	}
	if bytes.Count(jsonl, []byte{'\n'}) != 1 || jsonl[len(jsonl)-1] != '\n' {
		t.Fatalf("normalization must emit exactly one newline-terminated record: %q", jsonl)
	}

	wantDigest := sha256.Sum256(raw)
	if facts.samples != 16000 || facts.duration != time.Second || facts.durationMinutes != 1.0/60.0 ||
		facts.rawBytes != int64(len(raw)) || facts.sha256 != hex.EncodeToString(wantDigest[:]) {
		t.Fatalf("facts = %+v", facts)
	}

	jsonlAgain, factsAgain, err := normalizeAudioUploadWAV(append([]byte(nil), raw...))
	if err != nil {
		t.Fatalf("second normalization: %v", err)
	}
	if !bytes.Equal(jsonl, jsonlAgain) || facts != factsAgain {
		t.Fatalf("normalization is not deterministic: (%q, %+v) != (%q, %+v)", jsonl, facts, jsonlAgain, factsAgain)
	}
}

func TestParseAudioUploadWAVAcceptsBoundsAndValidOddPadding(t *testing.T) {
	tests := []struct {
		name    string
		raw     []byte
		samples int64
	}{
		{"one sample", validAudioUploadTestWAV(1), 1},
		{"maximum samples", validAudioUploadTestWAV(audioUploadMaxSamples), audioUploadMaxSamples},
		{"unknown odd chunk", audioUploadTestWAV(
			audioUploadTestChunk{id: "JUNK", payload: []byte{7}, pad: 0},
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		), 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			facts, err := parseAudioUploadWAV(tc.raw)
			if err != nil {
				t.Fatalf("parseAudioUploadWAV: %v", err)
			}
			if facts.samples != tc.samples {
				t.Fatalf("samples = %d, want %d", facts.samples, tc.samples)
			}
		})
	}
}

func TestParseAudioUploadWAVRejectsMalformedAndOutOfContract(t *testing.T) {
	valid := validAudioUploadTestWAV(2)
	formatMutation := func(offset int, put func([]byte)) []byte {
		format := audioUploadTestFormat()
		put(format[offset:])
		return audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: format},
			audioUploadTestChunk{id: "data", payload: make([]byte, 4)},
		)
	}
	riffSize := func(raw []byte, size uint32) []byte {
		out := append([]byte(nil), raw...)
		binary.LittleEndian.PutUint32(out[4:8], size)
		return out
	}
	chunkSize := func(raw []byte, headerOffset int, size uint32) []byte {
		out := append([]byte(nil), raw...)
		binary.LittleEndian.PutUint32(out[headerOffset+4:headerOffset+8], size)
		return out
	}

	missingOddPad := audioUploadTestWAV(
		audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
		audioUploadTestChunk{id: "JUNK", payload: []byte{1}},
		audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
	)
	// Delete JUNK's pad while making the outer RIFF length truthful. Its next
	// chunk's first byte must not be silently consumed as padding.
	missingOddPad = append(missingOddPad[:45], missingOddPad[46:]...)
	binary.LittleEndian.PutUint32(missingOddPad[4:8], uint32(len(missingOddPad)-8))
	missingFinalOddPad := audioUploadTestWAV(
		audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
		audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		audioUploadTestChunk{id: "JUNK", payload: []byte{1}},
	)
	missingFinalOddPad = missingFinalOddPad[:len(missingFinalOddPad)-1]
	binary.LittleEndian.PutUint32(missingFinalOddPad[4:8], uint32(len(missingFinalOddPad)-8))

	tests := []struct {
		name string
		raw  []byte
		want string
	}{
		{"nil", nil, "truncated RIFF/WAVE header"},
		{"short header", []byte("RIFF"), "truncated RIFF/WAVE header"},
		{"over raw byte limit", make([]byte, audioUploadMaxRawBytes+1), "exceeds"},
		{"bad RIFF", append([]byte("NOPE"), valid[4:]...), "missing RIFF"},
		{"bad WAVE", append(append([]byte(nil), valid[:8]...), append([]byte("AVI "), valid[12:]...)...), "must be WAVE"},
		{"RIFF declares less", riffSize(valid, uint32(len(valid)-9)), "RIFF size mismatch"},
		{"RIFF declares more", riffSize(valid, uint32(len(valid)-7)), "RIFF size mismatch"},
		{"RIFF uint32 maximum", riffSize(valid, ^uint32(0)), "RIFF size mismatch"},
		{"truncated chunk header", func() []byte {
			raw := append([]byte(nil), valid...)
			raw = append(raw, 'x')
			binary.LittleEndian.PutUint32(raw[4:8], uint32(len(raw)-8))
			return raw
		}(), "truncated chunk header"},
		{"chunk declares past end", chunkSize(valid, 12, ^uint32(0)), "chunk data is truncated"},
		{"missing odd padding", missingOddPad, "nonzero odd-byte padding"},
		{"missing final odd padding", missingFinalOddPad, "missing odd-byte padding"},
		{"nonzero odd padding", audioUploadTestWAV(
			audioUploadTestChunk{id: "JUNK", payload: []byte{1}, pad: 9},
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		), "nonzero odd-byte padding"},
		{"missing format", audioUploadTestWAV(audioUploadTestChunk{id: "data", payload: []byte{1, 2}}), "fmt chunk"},
		{"data before format", audioUploadTestWAV(
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
		), "fmt chunk must precede data"},
		{"missing data", audioUploadTestWAV(audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()}), "missing data"},
		{"duplicate format", audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		), "duplicate fmt"},
		{"duplicate data", audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
			audioUploadTestChunk{id: "data", payload: []byte{3, 4}},
		), "duplicate data"},
		{"short format", audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()[:15]},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		), "exactly 16"},
		{"extended format", audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: append(audioUploadTestFormat(), 0, 0)},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		), "exactly 16"},
		{"non PCM", formatMutation(0, func(b []byte) { binary.LittleEndian.PutUint16(b, 3) }), "format must be PCM"},
		{"stereo", formatMutation(2, func(b []byte) { binary.LittleEndian.PutUint16(b, 2) }), "channel count"},
		{"wrong sample rate", formatMutation(4, func(b []byte) { binary.LittleEndian.PutUint32(b, 44100) }), "sample rate"},
		{"wrong byte rate", formatMutation(8, func(b []byte) { binary.LittleEndian.PutUint32(b, 1) }), "byte rate"},
		{"wrong block alignment", formatMutation(12, func(b []byte) { binary.LittleEndian.PutUint16(b, 4) }), "block alignment"},
		{"wrong bit depth", formatMutation(14, func(b []byte) { binary.LittleEndian.PutUint16(b, 24) }), "bits per sample"},
		{"empty PCM", validAudioUploadTestWAV(0), "nonempty"},
		{"unaligned PCM", audioUploadTestWAV(
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1}, pad: 0},
		), "not block-aligned"},
		{"too many samples", validAudioUploadTestWAV(audioUploadMaxSamples + 1), "sample count exceeds"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			defer func() {
				if recovered := recover(); recovered != nil {
					t.Fatalf("parseAudioUploadWAV panicked: %v", recovered)
				}
			}()
			if _, err := parseAudioUploadWAV(tc.raw); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want substring %q", err, tc.want)
			}
			if jsonl, facts, err := normalizeAudioUploadWAV(tc.raw); err == nil || jsonl != nil || facts != (audioUploadFacts{}) {
				t.Fatalf("normalization on invalid input = (%q, %+v, %v), want nil/zero/error", jsonl, facts, err)
			}
		})
	}
}

func FuzzParseAudioUploadWAVNoPanic(f *testing.F) {
	valid := validAudioUploadTestWAV(8)
	for _, seed := range [][]byte{
		nil,
		[]byte("RIFF"),
		valid,
		validAudioUploadTestWAV(audioUploadMaxSamples),
		audioUploadTestWAV(
			audioUploadTestChunk{id: "JUNK", payload: []byte{1}, pad: 0},
			audioUploadTestChunk{id: "fmt ", payload: audioUploadTestFormat()},
			audioUploadTestChunk{id: "data", payload: []byte{1, 2}},
		),
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, raw []byte) {
		facts, parseErr := parseAudioUploadWAV(raw)
		jsonl, normalizedFacts, normalizeErr := normalizeAudioUploadWAV(raw)
		if parseErr != nil {
			if normalizeErr == nil || jsonl != nil || normalizedFacts != (audioUploadFacts{}) {
				t.Fatalf("invalid parse was normalized: parse=%v normalize=(%q, %+v, %v)", parseErr, jsonl, normalizedFacts, normalizeErr)
			}
			return
		}
		if normalizeErr != nil {
			t.Fatalf("valid parse failed normalization: %v", normalizeErr)
		}
		if facts != normalizedFacts || len(jsonl) == 0 || jsonl[len(jsonl)-1] != '\n' {
			t.Fatalf("normalization invariant failed: parse=%+v normalized=%+v jsonl=%q", facts, normalizedFacts, jsonl)
		}
	})
}
