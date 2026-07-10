//! M1 — wgpu + WGSL compute smoke test.
//!
//! Proves the GPU substrate the real dual-backend path tracer will sit on:
//! instance -> adapter -> device/queue -> WGSL compute pipeline -> storage
//! buffers -> dispatch -> map-read the result. This is NOT the path tracer yet;
//! it is the round-trip proof that Track-3 M1 requires before any renderer
//! kernel is worth writing.
//!
//! Backend selection is EXPLICIT and platform-driven:
//!   * Apple Silicon (this repo's owner Mac) -> request Metal.
//!   * RunPod NVIDIA box                      -> request Vulkan.
//! One binary, backend chosen at runtime — the wgpu equivalent of
//! agent/Cargo.toml's metal|cuda build-time split, and the whole point of M1.
//!
//! Honesty contract (mirrors pod/*.py runners): emits EXACTLY ONE final JSON
//! line to stdout. On success: {"ok":true,...measured facts...}. On ANY failure
//! (no adapter, shader error, wrong result): {"ok":false,"error":"..."} and a
//! nonzero exit. It never fabricates a "pass".
//!
//! Run:
//!   cargo run --example wgpu_smoke --features gpu                # auto backend
//!   CX_WGPU_BACKEND=vulkan cargo run --example wgpu_smoke --features gpu
//!   CX_WGPU_BACKEND=metal  cargo run --example wgpu_smoke --features gpu

use std::borrow::Cow;
use wgpu::util::DeviceExt;

/// Elements processed by the compute kernel.
const N: u32 = 1024;

/// The trivial kernel: out[i] = in[i] * 2 + i. Chosen because a correct result
/// depends on the workgroup id, the buffer binding, AND the arithmetic — a
/// silently-misbound buffer or a no-op dispatch produces the wrong numbers, so
/// "the readback matched" actually means the round-trip worked.
const SHADER: &str = r#"
@group(0) @binding(0) var<storage, read>        input  : array<f32>;
@group(0) @binding(1) var<storage, read_write>  output : array<f32>;

@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) gid : vec3<u32>) {
    let i = gid.x;
    if (i >= arrayLength(&input)) { return; }
    output[i] = input[i] * 2.0 + f32(i);
}
"#;

fn emit_err(msg: &str) -> ! {
    // one JSON line, then nonzero exit — never a fabricated pass.
    println!("{{\"ok\":false,\"stage\":\"wgpu_smoke\",\"error\":{}}}", json_str(msg));
    std::process::exit(1);
}

/// Minimal JSON string escaper (no serde dep for a diagnostic example).
fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn choose_backends() -> (wgpu::Backends, &'static str) {
    // Explicit override wins.
    if let Ok(v) = std::env::var("CX_WGPU_BACKEND") {
        match v.to_ascii_lowercase().as_str() {
            "metal" => return (wgpu::Backends::METAL, "metal(forced)"),
            "vulkan" => return (wgpu::Backends::VULKAN, "vulkan(forced)"),
            "dx12" => return (wgpu::Backends::DX12, "dx12(forced)"),
            "gl" => return (wgpu::Backends::GL, "gl(forced)"),
            other => emit_err(&format!("unknown CX_WGPU_BACKEND={other:?}")),
        }
    }
    // Platform default: Metal on Apple, Vulkan elsewhere (RunPod NVIDIA).
    if cfg!(target_os = "macos") {
        (wgpu::Backends::METAL, "metal")
    } else {
        (wgpu::Backends::VULKAN, "vulkan")
    }
}

fn run() -> Result<String, String> {
    let (backends, backend_label) = choose_backends();

    let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends,
        ..Default::default()
    });

    let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
        power_preference: wgpu::PowerPreference::HighPerformance,
        force_fallback_adapter: false,
        compatible_surface: None,
    }))
    .ok_or_else(|| format!("no {backend_label} adapter available"))?;

    let info = adapter.get_info();

    let (device, queue) = pollster::block_on(adapter.request_device(
        &wgpu::DeviceDescriptor {
            label: Some("cx-renderer-smoke"),
            required_features: wgpu::Features::empty(),
            required_limits: wgpu::Limits::downlevel_defaults(),
            memory_hints: wgpu::MemoryHints::Performance,
        },
        None,
    ))
    .map_err(|e| format!("request_device failed: {e}"))?;

    // Input data + expected output computed on the CPU for verification.
    let input: Vec<f32> = (0..N).map(|i| i as f32 * 0.5).collect();
    let expected: Vec<f32> = input.iter().enumerate().map(|(i, v)| v * 2.0 + i as f32).collect();

    let input_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("input"),
        contents: bytemuck::cast_slice(&input),
        usage: wgpu::BufferUsages::STORAGE,
    });
    let bytes = (N as usize * std::mem::size_of::<f32>()) as u64;
    let output_buf = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("output"),
        size: bytes,
        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
        mapped_at_creation: false,
    });
    let readback_buf = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("readback"),
        size: bytes,
        usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
        mapped_at_creation: false,
    });

    let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("smoke.wgsl"),
        source: wgpu::ShaderSource::Wgsl(Cow::Borrowed(SHADER)),
    });

    let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some("smoke-pipeline"),
        layout: None,
        module: &module,
        entry_point: "main",
        compilation_options: wgpu::PipelineCompilationOptions::default(),
        cache: None,
    });

    let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("smoke-bg"),
        layout: &pipeline.get_bind_group_layout(0),
        entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: input_buf.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: output_buf.as_entire_binding() },
        ],
    });

    let mut encoder =
        device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: Some("smoke-enc") });
    {
        let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
            label: Some("smoke-pass"),
            timestamp_writes: None,
        });
        pass.set_pipeline(&pipeline);
        pass.set_bind_group(0, &bind_group, &[]);
        pass.dispatch_workgroups((N + 63) / 64, 1, 1);
    }
    encoder.copy_buffer_to_buffer(&output_buf, 0, &readback_buf, 0, bytes);
    queue.submit(Some(encoder.finish()));

    // Map & read back.
    let slice = readback_buf.slice(..);
    let (tx, rx) = std::sync::mpsc::channel();
    slice.map_async(wgpu::MapMode::Read, move |r| {
        let _ = tx.send(r);
    });
    device.poll(wgpu::Maintain::Wait);
    rx.recv()
        .map_err(|_| "map_async callback dropped".to_string())?
        .map_err(|e| format!("buffer map failed: {e:?}"))?;

    let got: Vec<f32> = {
        let data = slice.get_mapped_range();
        bytemuck::cast_slice::<u8, f32>(&data).to_vec()
    };
    readback_buf.unmap();

    // Verify every element.
    let mut max_err = 0.0f32;
    let mut first_bad: Option<usize> = None;
    for (i, (&g, &e)) in got.iter().zip(expected.iter()).enumerate() {
        let err = (g - e).abs();
        if err > max_err {
            max_err = err;
        }
        if err > 1e-4 && first_bad.is_none() {
            first_bad = Some(i);
        }
    }
    if let Some(i) = first_bad {
        return Err(format!(
            "GPU result wrong at element {i}: got {}, expected {} (max_err {max_err:.3e})",
            got[i], expected[i]
        ));
    }

    let backend = format!("{:?}", info.backend);
    let device_type = format!("{:?}", info.device_type);
    Ok(format!(
        "{{\"ok\":true,\"stage\":\"wgpu_smoke\",\"requested_backend\":{},\"backend\":{},\
\"adapter\":{},\"device_type\":{},\"driver\":{},\"elements\":{},\"max_abs_err\":{:.3e}}}",
        json_str(backend_label),
        json_str(&backend),
        json_str(&info.name),
        json_str(&device_type),
        json_str(&info.driver_info),
        N,
        max_err
    ))
}

fn main() {
    match run() {
        Ok(line) => {
            println!("{line}");
        }
        Err(e) => emit_err(&e),
    }
}
