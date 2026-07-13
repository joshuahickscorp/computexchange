//! Device-free wall-time benchmark for the resident scheduler and completion
//! validator. Model execution is replaced by exact, immediate completions so
//! the result is the host-side orchestration floor.
//!
//! Usage:
//!   cargo run --release --example bench_resident_scheduler -- [requests] [reps]

#[path = "../src/resident_engine.rs"]
mod resident_engine;

use std::hint::black_box;
use std::time::Instant;

use resident_engine::{
    DispatchCompletion, EngineConfig, EngineTime, ItemOutput, RequestId, RequestSpec,
    ResidentEngine, WorkKind,
};

fn run(requests: usize) -> (usize, usize) {
    let config = EngineConfig {
        max_active_requests: requests,
        max_queued_requests: requests,
        token_budget_per_tick: 16_384,
        max_batch_items: 64,
        prefill_chunk_tokens: 128,
        starvation_ticks: 4,
        activation_quantum_tokens: 16_384,
        terminal_event_capacity: requests,
    };
    let mut engine = ResidentEngine::new(config).expect("valid benchmark config");
    for id in 0..requests {
        engine
            .admit(
                RequestSpec {
                    request_id: RequestId(id as u64),
                    prompt_tokens: 128,
                    max_new_tokens: 64,
                    deadline: None,
                },
                EngineTime(0),
            )
            .expect("benchmark admission");
    }

    let mut dispatches = 0;
    let mut work_items = 0;
    let mut tick = 0;
    while engine.snapshot().active != 0 || engine.snapshot().queued != 0 {
        while let Some(plan) = engine.plan(EngineTime(tick)).expect("plan") {
            dispatches += 1;
            work_items += plan.items().len();
            let items = plan
                .items()
                .iter()
                .map(|item| match *item.kind() {
                    WorkKind::Prefill { tokens, .. } => ItemOutput::Prefill {
                        handle: item.handle(),
                        processed_tokens: tokens,
                    },
                    WorkKind::Decode { position } => ItemOutput::Decode {
                        handle: item.handle(),
                        token: position as u32,
                        eos: false,
                    },
                })
                .collect();
            engine
                .apply_completion(
                    DispatchCompletion {
                        dispatch_id: plan.dispatch_id(),
                        items,
                    },
                    EngineTime(tick),
                )
                .expect("completion");
        }
        tick += 1;
    }
    (dispatches, work_items)
}

fn median(samples: &mut [f64]) -> f64 {
    samples.sort_by(f64::total_cmp);
    samples[samples.len() / 2]
}

fn main() {
    let mut args = std::env::args().skip(1);
    let requests = args
        .next()
        .map(|value| value.parse::<usize>().expect("requests must be an integer"))
        .unwrap_or(256);
    let reps = args
        .next()
        .map(|value| value.parse::<usize>().expect("reps must be an integer"))
        .unwrap_or(9);
    assert!(requests > 0 && reps > 0);

    black_box(run(requests));
    let mut samples_ms = Vec::with_capacity(reps);
    let mut work = (0, 0);
    for _ in 0..reps {
        let started = Instant::now();
        work = run(requests);
        samples_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
        black_box(work);
    }
    let median_ms = median(&mut samples_ms);
    println!(
        "{{\"kind\":\"resident_scheduler_overhead\",\"requests\":{requests},\"reps\":{reps},\"dispatches\":{},\"work_items\":{},\"median_ms\":{median_ms:.6},\"work_items_per_second\":{:.3},\"samples_ms\":{:?}}}",
        work.0,
        work.1,
        work.1 as f64 * 1_000.0 / median_ms,
        samples_ms,
    );
}
