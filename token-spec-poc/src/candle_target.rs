//! Real-model backend (feature `candle`).
//!
//! Loads a quantized-Llama GGUF with STOCK candle-transformers, greedy-decodes the
//! prompt to obtain the model's OWN greedy token stream, then runs the CX-owned
//! lossless spec-decode loop against that real stream. This MEASURES draft
//! acceptance and target-call reduction on REAL Llama-3.2-1B output — locally, on
//! Metal/CPU, with no fleet and no fork.
//!
//! Why this is honest and not circular: acceptance is the n-gram drafter's true
//! hit rate against the real model's greedy continuation. The one thing it can NOT
//! measure locally is wall-clock >1x, because verify-by-lookup is not a real
//! forward pass. Wall-clock needs `forward_all_logits` + `KvCacheSlot::truncate`
//! (the fork) and a fleet run — exactly as TOKEN_LANE_FORK_DESIGN.md states. So the
//! receipt keeps `walltime_label = "MODELED"` and reports only the MEASURED
//! acceptance/ceiling here.

use anyhow::{Context, Result};
use candle_core::quantized::gguf_file;
use candle_core::{Device, Tensor};
use candle_transformers::models::quantized_llama::ModelWeights;
use tokenizers::Tokenizer;

use token_spec_poc::{run_spec_decode, MockTarget, NgramDraft, SpecUnit};

fn device() -> Device {
    #[cfg(feature = "metal")]
    if let Ok(d) = Device::new_metal(0) {
        return d;
    }
    Device::Cpu
}

/// Greedy-decode `prompt` through the real model for up to `max_new` tokens,
/// returning (prompt_ids, greedy_continuation_ids, eos_id).
fn real_greedy_stream(
    gguf_path: &str,
    tokenizer: &Tokenizer,
    prompt: &str,
    max_new: usize,
) -> Result<(Vec<u32>, Vec<u32>, u32)> {
    let dev = device();
    let mut file =
        std::fs::File::open(gguf_path).with_context(|| format!("open gguf {gguf_path}"))?;
    let content = gguf_file::Content::read(&mut file).context("read gguf content")?;
    let eos = content
        .metadata
        .get("tokenizer.ggml.eos_token_id")
        .and_then(|v| v.to_u32().ok())
        .unwrap_or(2);
    let mut model = ModelWeights::from_gguf(content, &mut file, &dev).context("from_gguf")?;

    // Llama-3 chat wrap, matching the agent's LlamaBackend::generate.
    let wrapped = format!(
        "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|>\
         <|start_header_id|>assistant<|end_header_id|>\n\n"
    );
    let enc = tokenizer
        .encode(wrapped, true)
        .map_err(|e| anyhow::anyhow!("tokenize: {e}"))?;
    let prompt_ids: Vec<u32> = enc.get_ids().to_vec();

    let mut tokens = prompt_ids.clone();
    let mut generated: Vec<u32> = Vec::new();
    let mut index_pos = 0usize;
    for step in 0..max_new {
        let ctx = if step == 0 {
            &tokens[..]
        } else {
            &tokens[tokens.len() - 1..]
        };
        let input = Tensor::new(ctx, &dev)?.unsqueeze(0)?;
        let logits = model.forward(&input, index_pos)?;
        let logits = logits.squeeze(0)?;
        let last = if logits.rank() == 2 {
            let s = logits.dim(0)?;
            logits.get(s - 1)?
        } else {
            logits
        };
        let next = last.argmax(0)?.to_scalar::<u32>()?;
        index_pos += ctx.len();
        if next == eos {
            break;
        }
        tokens.push(next);
        generated.push(next);
    }
    Ok((prompt_ids, generated, eos))
}

/// Resolve a tokenizer: `--tokenizer <path>` if given, else fetch the base repo's
/// tokenizer.json via hf-hub (cached; the agent uses the same repo).
fn load_tokenizer() -> Result<Tokenizer> {
    // Base repo whose tokenizer matches the Llama-3.2-1B GGUF the agent ships.
    use hf_hub::api::sync::ApiBuilder;
    let api = ApiBuilder::new().build().context("hf-hub api")?;
    let repo = api.model("unsloth/Llama-3.2-1B-Instruct".to_string());
    let path = repo
        .get("tokenizer.json")
        .context("fetch tokenizer.json (needs it cached or network)")?;
    Tokenizer::from_file(path).map_err(|e| anyhow::anyhow!("load tokenizer: {e}"))
}

/// Public entry: measure real-model acceptance + emit a SpecReceipt JSON line.
pub fn measure_real_model(
    gguf_path: &str,
    prompt: &str,
    max_new: usize,
    k: usize,
    order: usize,
) -> Result<String> {
    let tokenizer = load_tokenizer()?;
    let (prompt_ids, truth, eos) = real_greedy_stream(gguf_path, &tokenizer, prompt, max_new)?;
    if truth.is_empty() {
        anyhow::bail!("model produced no tokens (empty greedy stream)");
    }
    let unit = SpecUnit {
        unit_id: "real_llama_1b".to_string(),
        modality: "token".to_string(),
        prompt: prompt_ids.clone(),
        max_new_tokens: truth.len(),
        eos,
    };
    // Real-model-derived target: greedy answers are the model's own stream.
    let mut target = MockTarget::new(prompt_ids.len(), {
        let mut s = truth.clone();
        s.push(eos); // so the loop can reach EOS naturally if it wants
        s
    });
    let mut draft = NgramDraft::try_new(order, 64).map_err(|e| anyhow::anyhow!(e))?;
    let out = run_spec_decode(&unit, &mut draft, &mut target, k, "token-spec-poc");
    let mut receipt = out.receipt;
    receipt.meta.target_backend = "candle_quantized_llama_1b".to_string();
    receipt.meta.notes = format!(
        "REAL Llama-3.2-1B greedy stream ({} tokens); acceptance MEASURED on real model output; \
         {}",
        truth.len(),
        receipt.meta.notes
    );
    Ok(serde_json::to_string(&receipt)?)
}
