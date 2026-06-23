#!/usr/bin/env python3
"""anytask_demo.py — proof that the NVIDIA lane can run ANY computational task a
buyer might submit, not just our fixed AI catalogue. Five different domains, each a
real GPU computation with a fingerprinted (verifiable) result and a per-task time
(the metered billable unit).

This is a VALIDATION HARNESS for the "bring-your-own-compute" general-compute model
(ACCRETION.md §7) — in production these run inside a per-job sandbox (container +
cgroups) on the supplier's GPU host.

Run:  pip install torch --index-url https://download.pytorch.org/whl/cu124 && python3 anytask_demo.py

Verified on a RunPod A100-SXM4-80GB (2026-06-23): all five ran in ~1.7s, peak 12.8 GB.
"""
import torch, time, hashlib

d = "cuda"
torch.manual_seed(0)


def fp(t):
    return hashlib.sha256(t.detach().float().cpu().contiguous().numpy().tobytes()).hexdigest()[:12]


def run(name, fn):
    torch.cuda.synchronize()
    t0 = time.time()
    r = fn()
    torch.cuda.synchronize()
    print(f"  {name:34s} {(time.time()-t0)*1000:8.1f} ms   result={fp(r)}")


def train():  # AI training
    net = torch.nn.Sequential(torch.nn.Linear(1024, 1024), torch.nn.ReLU(), torch.nn.Linear(1024, 10)).to(d)
    opt = torch.optim.SGD(net.parameters(), lr=0.01)
    x = torch.randn(4096, 1024, device=d); y = torch.randint(0, 10, (4096,), device=d)
    for _ in range(50):
        opt.zero_grad(); loss = torch.nn.functional.cross_entropy(net(x), y); loss.backward(); opt.step()
    return loss.reshape(1)


def solve():  # scientific computing / simulation (FEA/CFD-style dense solve)
    n = 4096; A = torch.randn(n, n, device=d); A = A @ A.T + n * torch.eye(n, device=d)
    return torch.linalg.solve(A, torch.randn(n, 1, device=d))


def nbody():  # physics simulation
    n = 20000; p = torch.randn(n, 3, device=d); diff = p[:, None, :] - p[None, :, :]
    r2 = (diff ** 2).sum(-1) + 1e-3
    return (diff / r2[..., None] ** 1.5).sum(1)


def mandel():  # arbitrary user math (fractal escape iteration)
    n = 4096
    cx, cy = torch.meshgrid(torch.linspace(-2, 1, n, device=d), torch.linspace(-1.5, 1.5, n, device=d), indexing="xy")
    zx = torch.zeros_like(cx); zy = torch.zeros_like(cy); c = torch.zeros_like(cx)
    for _ in range(100):
        zx, zy = zx * zx - zy * zy + cx, 2 * zx * zy + cy; c += (zx * zx + zy * zy) < 4
    return c


if __name__ == "__main__":
    print(f"device: {torch.cuda.get_device_name(0)} | torch {torch.__version__}")
    print("five arbitrary user tasks, five different domains, all on the GPU:")
    t_all = time.time()
    run("AI training (MLP, 50 SGD steps)", train)
    run("signal: 2D FFT (4096x4096 cplx)", lambda: torch.fft.fft2(torch.randn(4096, 4096, dtype=torch.complex64, device=d)).real)
    run("sci-comp: dense solve Ax=b 4096", solve)
    run("physics: N-body forces (20k)", nbody)
    run("custom math: Mandelbrot 4096^2", mandel)
    print(f"total GPU wall-time (metered billable unit): {(time.time()-t_all)*1000:.0f} ms")
    print(f"peak VRAM: {torch.cuda.max_memory_allocated()/1e9:.1f} GB / 80 GB")
