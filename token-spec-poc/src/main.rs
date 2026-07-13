//! `token-spec-poc` harness.
//!
//! Default (no features): runs the CX-owned lossless spec-decode loop over a set
//! of REPRESENTATIVE byte-token streams (code / json / prose / repeat / random)
//! with the n-gram prompt-lookup drafter and a mock target whose greedy stream IS
//! the stream. This MEASURES, locally and dependency-free:
//!   * losslessness (every emitted stream equals the target greedy stream),
//!   * draft ACCEPTANCE rate, and
//!   * TARGET-CALL REDUCTION (the speedup CEILING).
//!
//! It does NOT claim a wall-clock speedup — `speedup_x` is labeled MODELED.
//!
//! With `--features candle --gguf <path> --prompt <text>`: swaps the mock target
//! for a REAL quantized-Llama greedy stream (the model's own greedy decode,
//! computed once with stock candle), so acceptance is measured against real model
//! output. Wall-clock >1x still needs the fork (see TOKEN_LANE_FORK_DESIGN.md).
//!
//! Output: one JSON object per stream on stdout (a JSONL receipt table).

use token_spec_poc::{run_spec_decode, MockTarget, NgramDraft, SpecUnit};

fn byte_tokens(s: &str) -> Vec<u32> {
    s.bytes().map(|b| b as u32).collect()
}

/// Representative streams, mirroring the repo's prior stream taxonomy so numbers
/// are comparable — but now driven through the CX-owned lossless loop.
fn streams() -> Vec<(&'static str, Vec<u32>)> {
    let code = r#"
fn compact(&mut self, idx: &Tensor) -> Result<()> {
    if let Some(buf) = self.buf.as_ref() {
        let live = buf.narrow(2, 0, self.cur_len)?.contiguous()?;
        let kept = live.index_select(idx, 0)?;
        let alloc_len = self.cap.max(self.cur_len).min(MAX_SEQ_LEN);
        let new_buf = Tensor::zeros((b_sz, n_kv_head, alloc_len, head_dim), dt, dev)?;
        new_buf.slice_set(&kept.contiguous()?, 2, 0)?;
        self.buf = Some(new_buf);
    }
    Ok(())
}
"#;
    let json = r#"{"branch_id":"token-spec-poc","modality":"token","units":128,"accepted_units":96,"accepted_fraction":0.75,"exact":true,"quality_gate":true,"meta":{"draft_producer":"ngram_prompt_lookup","target_backend":"mock_fixed_stream","walltime_label":"MODELED"}}"#;
    let prose = "The quick brown fox jumps over the lazy dog. Prediction is compression; a good predictor of the next token is a good codec for the whole stream. The quick brown fox jumps over the lazy dog again, and the predictor that saw it once will draft it the second time.";
    let repeat = {
        let pat = "abcdefgh";
        pat.repeat(32)
    };
    // High-entropy negative control: acceptance should collapse toward zero.
    let random: Vec<u32> = {
        let mut x: u64 = 0x9E3779B97F4A7C15;
        (0..256)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x % 256) as u32
            })
            .collect()
    };
    vec![
        ("code", byte_tokens(code)),
        ("json", byte_tokens(json)),
        ("prose", byte_tokens(prose)),
        ("repeat", byte_tokens(&repeat)),
        ("random", random),
    ]
}

fn run_stream_mock(name: &str, stream: &[u32], k: usize, order: usize) -> String {
    // Prompt = first few tokens; the model "greedily" continues with the rest.
    let prompt_len = order.min(stream.len().saturating_sub(1)).max(1);
    let prompt = stream[..prompt_len].to_vec();
    let truth = stream[prompt_len..].to_vec();
    let unit = SpecUnit {
        unit_id: name.to_string(),
        modality: "token".to_string(),
        prompt,
        max_new_tokens: truth.len(),
        eos: u32::MAX, // no EOS in these byte streams
    };
    let mut target = MockTarget::new(prompt_len, truth);
    let mut draft = NgramDraft::new(order, 64);
    let out = run_spec_decode(&unit, &mut draft, &mut target, k, "token-spec-poc");
    let mut receipt = out.receipt;
    receipt.meta.notes = format!("stream={name}; order={order}; {}", receipt.meta.notes);
    serde_json::to_string(&receipt).unwrap()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let k = arg_val(&args, "--k")
        .and_then(|v| v.parse().ok())
        .unwrap_or(16usize);
    let order = arg_val(&args, "--order")
        .and_then(|v| v.parse().ok())
        .unwrap_or(3usize);
    if k == 0 || k > token_spec_poc::MAX_DRAFT_WINDOW {
        eprintln!("--k must be in [1, {}]", token_spec_poc::MAX_DRAFT_WINDOW);
        std::process::exit(2);
    }
    if order == 0 || order > token_spec_poc::MAX_NGRAM_ORDER {
        eprintln!(
            "--order must be in [1, {}]",
            token_spec_poc::MAX_NGRAM_ORDER
        );
        std::process::exit(2);
    }

    #[cfg(feature = "candle")]
    {
        if let Some(gguf) = arg_val(&args, "--gguf") {
            let prompt = arg_val(&args, "--prompt")
                .unwrap_or_else(|| "Explain speculative decoding in one paragraph.".to_string());
            let max_new = arg_val(&args, "--max-new")
                .and_then(|v| v.parse().ok())
                .unwrap_or(128usize);
            match candle_target::measure_real_model(&gguf, &prompt, max_new, k, order) {
                Ok(line) => {
                    println!("{line}");
                    return;
                }
                Err(e) => {
                    eprintln!("candle backend error: {e:#}");
                    std::process::exit(1);
                }
            }
        }
    }

    eprintln!(
        "token-spec-poc: lossless CX-owned spec-decode over representative byte streams\n\
         (k={k}, order={order}). accepted_fraction + target_call_reduction_x are MEASURED; \
         speedup_x is MODELED (needs the fork — see TOKEN_LANE_FORK_DESIGN.md).\n"
    );
    for (name, stream) in streams() {
        println!("{}", run_stream_mock(name, &stream, k, order));
    }
}

fn arg_val(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

#[cfg(feature = "candle")]
mod candle_target;
