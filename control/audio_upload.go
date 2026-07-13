package main

// audio_upload.go is the pure intake boundary used by the authenticated audio
// upload endpoints in audio_http.go. It deliberately contains no HTTP, storage,
// pricing, or job-creation code. The only accepted wire format is the exact
// bounded PCM WAV the Whisper worker consumes; normalization then produces one
// deterministic JSONL record for that worker.

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const (
	audioUploadMaxRawBytes = 1 << 20
	audioUploadSampleRate  = 16000
	audioUploadMaxSamples  = 480000
	audioUploadBlockAlign  = 2
)

// audioUploadFacts are private, server-derived authority about an accepted
// upload. A later endpoint can use these facts without trusting buyer-supplied
// duration, byte counts, or digests.
type audioUploadFacts struct {
	samples         int64
	duration        time.Duration
	durationMinutes float64
	rawBytes        int64
	sha256          string
}

// parseAudioUploadWAV validates raw as one complete, canonical PCM WAV and
// derives facts from the bytes themselves. The RIFF and chunk-size arithmetic
// is performed as uint64 and checked before any slice or int conversion.
func parseAudioUploadWAV(raw []byte) (audioUploadFacts, error) {
	var facts audioUploadFacts
	if len(raw) > audioUploadMaxRawBytes {
		return facts, fmt.Errorf("audio upload: WAV exceeds %d-byte limit", audioUploadMaxRawBytes)
	}
	if len(raw) < 12 {
		return facts, errors.New("audio upload: truncated RIFF/WAVE header")
	}
	if string(raw[0:4]) != "RIFF" {
		return facts, errors.New("audio upload: missing RIFF signature")
	}
	if string(raw[8:12]) != "WAVE" {
		return facts, errors.New("audio upload: RIFF form must be WAVE")
	}

	// RIFF size is the exact number of bytes after its size field. Requiring an
	// exact match rejects both truncation and unparsed trailing material.
	riffEnd := uint64(binary.LittleEndian.Uint32(raw[4:8])) + 8
	if riffEnd != uint64(len(raw)) {
		return facts, fmt.Errorf("audio upload: RIFF size mismatch: declares %d bytes, got %d", riffEnd, len(raw))
	}

	var (
		seenFormat bool
		seenData   bool
		dataBytes  uint64
	)
	for offset := uint64(12); offset < riffEnd; {
		if riffEnd-offset < 8 {
			return facts, errors.New("audio upload: truncated chunk header")
		}
		chunkID := string(raw[int(offset):int(offset+4)])
		chunkBytes := uint64(binary.LittleEndian.Uint32(raw[int(offset+4):int(offset+8)]))
		dataStart := offset + 8
		dataEnd, ok := checkedAudioUploadAdd(dataStart, chunkBytes)
		if !ok || dataEnd > riffEnd {
			return facts, fmt.Errorf("audio upload: %q chunk data is truncated", chunkID)
		}
		paddedEnd, ok := checkedAudioUploadAdd(dataEnd, chunkBytes&1)
		if !ok || paddedEnd > riffEnd {
			return facts, fmt.Errorf("audio upload: %q chunk is missing odd-byte padding", chunkID)
		}
		if chunkBytes&1 != 0 && raw[int(dataEnd)] != 0 {
			return facts, fmt.Errorf("audio upload: %q chunk has nonzero odd-byte padding", chunkID)
		}

		switch chunkID {
		case "fmt ":
			if seenFormat {
				return facts, errors.New("audio upload: duplicate fmt chunk")
			}
			seenFormat = true
			if chunkBytes != 16 {
				return facts, fmt.Errorf("audio upload: fmt chunk must be exactly 16 bytes, got %d", chunkBytes)
			}
			format := raw[int(dataStart):int(dataEnd)]
			if got := binary.LittleEndian.Uint16(format[0:2]); got != 1 {
				return facts, fmt.Errorf("audio upload: format must be PCM (1), got %d", got)
			}
			if got := binary.LittleEndian.Uint16(format[2:4]); got != 1 {
				return facts, fmt.Errorf("audio upload: channel count must be 1, got %d", got)
			}
			if got := binary.LittleEndian.Uint32(format[4:8]); got != audioUploadSampleRate {
				return facts, fmt.Errorf("audio upload: sample rate must be %d Hz, got %d", audioUploadSampleRate, got)
			}
			if got := binary.LittleEndian.Uint32(format[8:12]); got != audioUploadSampleRate*audioUploadBlockAlign {
				return facts, fmt.Errorf("audio upload: byte rate must be %d, got %d", audioUploadSampleRate*audioUploadBlockAlign, got)
			}
			if got := binary.LittleEndian.Uint16(format[12:14]); got != audioUploadBlockAlign {
				return facts, fmt.Errorf("audio upload: block alignment must be %d, got %d", audioUploadBlockAlign, got)
			}
			if got := binary.LittleEndian.Uint16(format[14:16]); got != 16 {
				return facts, fmt.Errorf("audio upload: bits per sample must be 16, got %d", got)
			}

		case "data":
			if seenData {
				return facts, errors.New("audio upload: duplicate data chunk")
			}
			// The WAVE contract (and the worker's decoder) requires the format
			// declaration before sample bytes. Do not normalize a container the
			// downstream reader would later reject as missing its format.
			if !seenFormat {
				return facts, errors.New("audio upload: fmt chunk must precede data chunk")
			}
			seenData = true
			dataBytes = chunkBytes
		}

		offset = paddedEnd
	}

	if !seenFormat {
		return facts, errors.New("audio upload: missing fmt chunk")
	}
	if !seenData {
		return facts, errors.New("audio upload: missing data chunk")
	}
	if dataBytes == 0 {
		return facts, errors.New("audio upload: PCM data must be nonempty")
	}
	if dataBytes%audioUploadBlockAlign != 0 {
		return facts, fmt.Errorf("audio upload: PCM data size %d is not block-aligned", dataBytes)
	}
	samples := dataBytes / audioUploadBlockAlign
	if samples > audioUploadMaxSamples {
		return facts, fmt.Errorf("audio upload: sample count exceeds %d-sample limit", audioUploadMaxSamples)
	}

	digest := sha256.Sum256(raw)
	facts = audioUploadFacts{
		samples:         int64(samples),
		duration:        time.Duration(samples) * (time.Second / audioUploadSampleRate),
		durationMinutes: float64(samples) / float64(audioUploadSampleRate*60),
		rawBytes:        int64(len(raw)),
		sha256:          hex.EncodeToString(digest[:]),
	}
	return facts, nil
}

// normalizeAudioUploadWAV validates raw and emits exactly one newline-terminated
// JSON object. A typed struct fixes the only key and encoding/json fixes its
// compact representation; StdEncoding supplies canonical padded base64.
func normalizeAudioUploadWAV(raw []byte) ([]byte, audioUploadFacts, error) {
	facts, err := parseAudioUploadWAV(raw)
	if err != nil {
		return nil, audioUploadFacts{}, err
	}
	record := struct {
		AudioBase64 string `json:"audio_b64"`
	}{
		AudioBase64: base64.StdEncoding.EncodeToString(raw),
	}
	jsonl, err := json.Marshal(record)
	if err != nil {
		return nil, audioUploadFacts{}, fmt.Errorf("audio upload: encode normalized JSONL: %w", err)
	}
	jsonl = append(jsonl, '\n')
	return jsonl, facts, nil
}

func checkedAudioUploadAdd(a, b uint64) (uint64, bool) {
	if ^uint64(0)-a < b {
		return 0, false
	}
	return a + b, true
}
