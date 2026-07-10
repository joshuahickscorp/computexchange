//! PCG32 — a small, fast, well-distributed, zero-dependency PRNG
//! (O'Neill 2014, `pcg-random.org`). We seed it deterministically per pixel so
//! the furnace test is bit-reproducible across runs and machines: a flaky
//! correctness gate is worthless.

#[derive(Clone)]
pub struct Pcg32 {
    state: u64,
    inc: u64,
}

impl Pcg32 {
    /// `seed` picks the position in the stream; `seq` picks the stream (must be
    /// distinct per independent generator to avoid correlation).
    pub fn new(seed: u64, seq: u64) -> Self {
        let mut r = Pcg32 { state: 0, inc: (seq << 1) | 1 };
        let _ = r.next_u32();
        r.state = r.state.wrapping_add(seed);
        let _ = r.next_u32();
        r
    }

    #[inline]
    pub fn next_u32(&mut self) -> u32 {
        let old = self.state;
        self.state = old
            .wrapping_mul(6364136223846793005)
            .wrapping_add(self.inc);
        let xorshifted = (((old >> 18) ^ old) >> 27) as u32;
        let rot = (old >> 59) as u32;
        xorshifted.rotate_right(rot)
    }

    /// Uniform float in [0, 1). 24 bits of mantissa — plenty for path sampling.
    #[inline]
    pub fn next_f32(&mut self) -> f32 {
        (self.next_u32() >> 8) as f32 / (1u32 << 24) as f32
    }
}
