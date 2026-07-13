package main

import (
	"math"
	"strings"
	"testing"
)

func productionMetalCapability() WorkerCapability {
	return WorkerCapability{
		HWClass:  "apple_silicon_pro",
		Engine:   "candle",
		MemoryGB: 36,
		SupportedJobs: []string{
			"embed", "rerank", "batch_infer", "batch_classification",
			"json_extraction", "audio_transcribe",
		},
		SupportedModels: []string{
			"all-minilm-l6-v2", "llama-3.2-1b-instruct-q4",
			"whisper-tiny", "whisper-base",
		},
		Benchmarks: []BenchResult{
			{JobType: "embed", ModelID: "all-minilm-l6-v2", EPS: 10, ThermalOK: true},
			{JobType: "batch_infer", ModelID: "llama-3.2-1b-instruct-q4", TPS: 10, ThermalOK: true},
		},
	}
}

func TestWorkerRuntimeProjectionRejectsHostileTelemetryAndIdentity(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*WorkerCapability)
		pattern string
	}{
		{
			name: "duplicate benchmark tuple",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks = append(c.Benchmarks, c.Benchmarks[0])
			},
			pattern: "duplicate tuple",
		},
		{
			name: "benchmark cardinality exceeds exact cells",
			mutate: func(c *WorkerCapability) {
				for len(c.Benchmarks) <= 7 {
					c.Benchmarks = append(c.Benchmarks, c.Benchmarks[0])
				}
			},
			pattern: "projects to only 7 exact production cells",
		},
		{
			name: "negative throughput",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].EPS = -1
			},
			pattern: "non-finite or negative",
		},
		{
			name: "nan throughput",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].EPS = float32(math.NaN())
			},
			pattern: "non-finite or negative",
		},
		{
			name: "zero native throughput",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].EPS = 0
				c.Benchmarks[0].TPS = 100
			},
			pattern: "no positive measured throughput",
		},
		{
			name: "implausibly high throughput",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].EPS = maxBenchmarkRate * 2
			},
			pattern: "plausible throughput maximum",
		},
		{
			name: "load uint64 overflow",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].LoadMS = math.MaxUint64
			},
			pattern: "load_ms",
		},
		{
			name: "p99 operational overflow",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks[0].P99MS = maxBenchmarkP99MS + 1
			},
			pattern: "p99_ms",
		},
		{
			name: "oversized build hash",
			mutate: func(c *WorkerCapability) {
				c.BuildHash = strings.Repeat("a", maxWorkerBuildHashBytes+1)
			},
			pattern: "build_hash exceeds",
		},
		{
			name: "version control character",
			mutate: func(c *WorkerCapability) {
				c.AgentVersion = "v1\nforged"
			},
			pattern: "control character",
		},
		{
			name: "nonfinite memory",
			mutate: func(c *WorkerCapability) {
				c.MemoryGB = float32(math.Inf(1))
			},
			pattern: "memory_gb",
		},
		{
			name: "absurd bandwidth",
			mutate: func(c *WorkerCapability) {
				c.MemoryBwGbps = maxWorkerMemoryBwGbps + 1
			},
			pattern: "memory_bw_gbps",
		},
		{
			name: "negative payout floor",
			mutate: func(c *WorkerCapability) {
				c.MinPayoutUsdHr = -1
			},
			pattern: "min_payout_usd_hr",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cap := productionMetalCapability()
			tc.mutate(&cap)
			err := validateWorkerRuntimeProjection(cap)
			if err == nil || !strings.Contains(err.Error(), tc.pattern) {
				t.Fatalf("error=%v, want substring %q", err, tc.pattern)
			}
		})
	}
}

func TestAdvertisedRuntimeJobModelIsExactNotCartesian(t *testing.T) {
	allowed := [][2]string{
		{"embed", "all-minilm-l6-v2"},
		{"rerank", "all-minilm-l6-v2"},
		{"batch_infer", "llama-3.2-1b-instruct-q4"},
		{"batch_classification", "llama-3.2-1b-instruct-q4"},
		{"json_extraction", "llama-3.2-1b-instruct-q4"},
		{"audio_transcribe", "whisper-tiny"},
		{"audio_transcribe", "whisper-base"},
	}
	for _, pair := range allowed {
		if err := validateAdvertisedRuntimeJobModel(pair[0], pair[1]); err != nil {
			t.Errorf("production tuple %q/%q rejected: %v", pair[0], pair[1], err)
		}
	}

	rejected := [][2]string{
		{"embed", "llama-3.2-1b-instruct-q4"},         // old Cartesian false positive
		{"batch_infer", "whisper-base"},               // old Cartesian false positive
		{"audio_transcribe", "all-minilm-l6-v2"},      // old Cartesian false positive
		{"batch_infer", "qwen2.5-7b-instruct-q4"},     // hardware_pending
		{"embed", "bge-small-en-v1.5"},                // hardware_pending
		{"custom", ""},                                // hardware_pending/model-less
		{"image_gen", "llama-3.2-1b-instruct-q4"},     // wire_only
		{"batch_infer", "llama-3.1-405b-instruct-q4"}, // stub cluster
	}
	for _, pair := range rejected {
		if err := validateAdvertisedRuntimeJobModel(pair[0], pair[1]); err == nil {
			t.Errorf("non-production tuple %q/%q was admitted", pair[0], pair[1])
		}
	}
}

func TestGeneratedRuntimeModelRefOwnsInternalWireKind(t *testing.T) {
	for _, tc := range []struct {
		job, model, kind string
	}{
		{"embed", "all-minilm-l6-v2", "hf"},
		{"batch_infer", "llama-3.2-1b-instruct-q4", "gguf"},
		{"audio_transcribe", "whisper-tiny", "hf"},
		{"unknown", "unknown", ""},
	} {
		got := generatedRuntimeModelRef(tc.job, tc.model)
		if got.Ref != tc.model || got.Kind != tc.kind {
			t.Fatalf("generatedRuntimeModelRef(%q,%q)=%+v, want kind %q", tc.job, tc.model, got, tc.kind)
		}
	}
}

func TestNormalizeAdvertisedRuntimeModelRefOwnsBuyerIngressKind(t *testing.T) {
	t.Run("omitted kind is canonicalized", func(t *testing.T) {
		got, err := normalizeAdvertisedRuntimeModelRef("embed", ModelRef{Ref: "all-minilm-l6-v2"})
		if err != nil {
			t.Fatalf("omitted kind rejected: %v", err)
		}
		if got != (ModelRef{Kind: "hf", Ref: "all-minilm-l6-v2"}) {
			t.Fatalf("normalized ref=%+v, want generated hf kind", got)
		}
	})

	t.Run("matching explicit kind remains canonical", func(t *testing.T) {
		got, err := normalizeAdvertisedRuntimeModelRef("batch_infer", ModelRef{
			Kind: "gguf",
			Ref:  "llama-3.2-1b-instruct-q4",
		})
		if err != nil {
			t.Fatalf("matching kind rejected: %v", err)
		}
		if got.Kind != "gguf" {
			t.Fatalf("normalized kind=%q, want gguf", got.Kind)
		}
	})

	t.Run("explicit mismatch is rejected", func(t *testing.T) {
		_, err := normalizeAdvertisedRuntimeModelRef("embed", ModelRef{
			Kind: "gguf",
			Ref:  "all-minilm-l6-v2",
		})
		if err == nil || !strings.Contains(err.Error(), `requires model.kind="hf"`) {
			t.Fatalf("mismatch error=%v, want generated-kind rejection", err)
		}
	})
}

func TestWorkerRegistrationConsumesProductionRuntimeProjection(t *testing.T) {
	valid := productionMetalCapability()
	if err := validateWorkerRuntimeProjection(valid); err != nil {
		t.Fatalf("valid production Metal worker rejected: %v", err)
	}
	projected, err := projectWorkerRuntimeCapabilities(valid)
	if err != nil {
		t.Fatalf("project valid production Metal worker: %v", err)
	}
	if len(projected) != 7 {
		t.Fatalf("6 jobs x 4 models must project to 7 exact production cells, got %d", len(projected))
	}
	seen := map[[2]string]bool{}
	for _, cell := range projected {
		seen[[2]string{cell.Job, cell.Model}] = true
		if cell.Runtime != "candle_metal" || cell.Engine != "candle" {
			t.Errorf("wrong runtime lane entered projection: %+v", cell)
		}
	}
	for _, falseCartesian := range [][2]string{
		{"embed", "llama-3.2-1b-instruct-q4"},
		{"batch_infer", "all-minilm-l6-v2"},
		{"audio_transcribe", "llama-3.2-1b-instruct-q4"},
	} {
		if seen[falseCartesian] {
			t.Errorf("unsupported Cartesian pair entered exact projection: %v", falseCartesian)
		}
	}

	cases := []struct {
		name    string
		mutate  func(*WorkerCapability)
		pattern string
	}{
		{
			name: "soak-only vllm cuda",
			mutate: func(c *WorkerCapability) {
				c.Engine, c.HWClass = "vllm", "nvidia_80g"
			},
			pattern: "no advertised production cell",
		},
		{
			name: "stub mlx metal",
			mutate: func(c *WorkerCapability) {
				c.Engine = "mlx"
			},
			pattern: "no advertised production cell",
		},
		{
			name: "hardware-pending qwen",
			mutate: func(c *WorkerCapability) {
				c.SupportedModels = append(c.SupportedModels, "qwen2.5-7b-instruct-q4")
			},
			pattern: "not advertised",
		},
		{
			name: "unsupported job",
			mutate: func(c *WorkerCapability) {
				c.SupportedJobs = append(c.SupportedJobs, "custom")
			},
			pattern: "not advertised",
		},
		{
			name: "benchmark cross product",
			mutate: func(c *WorkerCapability) {
				c.Benchmarks = append(c.Benchmarks, BenchResult{JobType: "batch_infer", ModelID: "whisper-base", TPS: 1})
			},
			pattern: "not an advertised production cell",
		},
		{
			name: "duplicate model",
			mutate: func(c *WorkerCapability) {
				c.SupportedModels = append(c.SupportedModels, "whisper-base")
			},
			pattern: "duplicate",
		},
		{
			name: "impossible memory claim",
			mutate: func(c *WorkerCapability) {
				c.MemoryGB = 1
			},
			pattern: "below advertised cell",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cap := productionMetalCapability()
			tc.mutate(&cap)
			err := validateWorkerRuntimeProjection(cap)
			if err == nil || !strings.Contains(err.Error(), tc.pattern) {
				t.Fatalf("error=%v, want substring %q", err, tc.pattern)
			}
		})
	}
}

func TestHeartbeatLoadedModelsStayInsideProductionProjection(t *testing.T) {
	if err := validateHeartbeatRuntimeModels([]string{"all-minilm-l6-v2", "whisper-tiny"}); err != nil {
		t.Fatalf("production warm models rejected: %v", err)
	}
	for _, models := range [][]string{
		{"qwen2.5-7b-instruct-q4"},
		{"unknown"},
		{"whisper-tiny", "whisper-tiny"},
	} {
		if err := validateHeartbeatRuntimeModels(models); err == nil {
			t.Fatalf("non-authoritative warm-model advertisement accepted: %v", models)
		}
	}
}

func productionCatalogRows() []ModelRow {
	return []ModelRow{
		{ID: "all-minilm-l6-v2", Kind: "embed", PricePer1K: .001, MinMemoryGB: 2, HFRepo: "sentence-transformers/all-MiniLM-L6-v2"},
		{ID: "llama-3.2-1b-instruct-q4", Kind: "gguf", PricePer1K: .002, MinMemoryGB: 4, HFRepo: "unsloth/Llama-3.2-1B-Instruct-GGUF"},
		{ID: "whisper-tiny", Kind: "whisper", PricePerUnit: .004, MinMemoryGB: 1, HFRepo: "openai/whisper-tiny"},
		{ID: "whisper-base", Kind: "whisper", PricePerUnit: .005, MinMemoryGB: 2, HFRepo: "openai/whisper-base"},
	}
}

func TestAdvertisedRuntimeCatalogFailsClosedOnDrift(t *testing.T) {
	if err := validateAdvertisedRuntimeCatalogRows(productionCatalogRows()); err != nil {
		t.Fatalf("valid production catalog rejected: %v", err)
	}

	t.Run("missing row", func(t *testing.T) {
		rows := productionCatalogRows()[:3]
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "no row") {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("zero price", func(t *testing.T) {
		rows := productionCatalogRows()
		rows[0].PricePer1K = 0
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "price") {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("understated memory", func(t *testing.T) {
		rows := productionCatalogRows()
		rows[1].MinMemoryGB = 3
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "requires") {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("missing resolver metadata", func(t *testing.T) {
		rows := productionCatalogRows()
		rows[2].HFRepo = ""
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "metadata") {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("unmapped catalog kind", func(t *testing.T) {
		rows := productionCatalogRows()
		rows[0].Kind = "opaque"
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "wire mapping") {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("supported but wrong wire kind", func(t *testing.T) {
		rows := productionCatalogRows()
		rows[0].Kind = "gguf"
		if err := validateAdvertisedRuntimeCatalogRows(rows); err == nil || !strings.Contains(err.Error(), "requires wire kind") {
			t.Fatalf("error=%v", err)
		}
	})
}

func TestGeneratedRuntimeCapabilitiesBindCanonicalWireKind(t *testing.T) {
	want := map[string]string{
		"all-minilm-l6-v2":         "hf",
		"llama-3.2-1b-instruct-q4": "gguf",
		"whisper-tiny":             "hf",
		"whisper-base":             "hf",
	}
	for _, cap := range generatedAdvertisedRuntimeCapabilities {
		if cap.ModelKind == "" {
			t.Fatalf("advertised cell %q has no generated model kind", cap.ID)
		}
		if cap.ModelKind != want[cap.Model] {
			t.Fatalf("cell %q model %q kind=%q, want %q", cap.ID, cap.Model, cap.ModelKind, want[cap.Model])
		}
	}
}

func TestRuntimeWireModelKind(t *testing.T) {
	for catalog, want := range map[string]string{
		"gguf": "gguf", "mlx": "mlx", "hf": "hf", "embed": "hf", "whisper": "hf",
	} {
		got, err := runtimeWireModelKind(catalog)
		if err != nil || got != want {
			t.Fatalf("runtimeWireModelKind(%q)=(%q,%v), want %q", catalog, got, err, want)
		}
	}
	if _, err := runtimeWireModelKind("container"); err == nil {
		t.Fatal("unmapped catalog kind must fail closed")
	}
}
