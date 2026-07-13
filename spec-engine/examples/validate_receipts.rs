//! Strict JSONL ingress checker used by local/CI cross-language gates.

use cx_spec_engine::SpecReceipt;
use std::io::{self, BufRead};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut count = 0usize;
    for (index, line) in stdin.lock().lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        SpecReceipt::from_json(&line)
            .map_err(|err| format!("receipt line {} failed strict ingress: {err}", index + 1))?;
        count += 1;
    }
    if count == 0 {
        return Err("no receipts supplied".into());
    }
    println!("strictly validated {count} receipt(s)");
    Ok(())
}
