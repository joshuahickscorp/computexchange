//! Minimal 3-component vector math. Zero dependencies on purpose — the whole
//! M0 furnace core must be auditable and buildable with nothing fetched.

use std::ops::{Add, Div, Mul, Neg, Sub};

#[derive(Copy, Clone, Debug, PartialEq)]
pub struct Vec3 {
    pub x: f32,
    pub y: f32,
    pub z: f32,
}

#[inline]
pub const fn vec3(x: f32, y: f32, z: f32) -> Vec3 {
    Vec3 { x, y, z }
}

impl Vec3 {
    pub const ZERO: Vec3 = Vec3 { x: 0.0, y: 0.0, z: 0.0 };
    pub const ONE: Vec3 = Vec3 { x: 1.0, y: 1.0, z: 1.0 };

    #[inline]
    pub fn splat(v: f32) -> Vec3 {
        Vec3 { x: v, y: v, z: v }
    }

    #[inline]
    pub fn dot(self, o: Vec3) -> f32 {
        self.x * o.x + self.y * o.y + self.z * o.z
    }

    #[inline]
    pub fn cross(self, o: Vec3) -> Vec3 {
        Vec3 {
            x: self.y * o.z - self.z * o.y,
            y: self.z * o.x - self.x * o.z,
            z: self.x * o.y - self.y * o.x,
        }
    }

    #[inline]
    pub fn length_squared(self) -> f32 {
        self.dot(self)
    }

    #[inline]
    pub fn length(self) -> f32 {
        self.length_squared().sqrt()
    }

    #[inline]
    pub fn normalize(self) -> Vec3 {
        self / self.length()
    }

    /// Component-wise multiply (Hadamard) — used for throughput * albedo and
    /// throughput * environment radiance.
    #[inline]
    pub fn mul_comp(self, o: Vec3) -> Vec3 {
        Vec3 {
            x: self.x * o.x,
            y: self.y * o.y,
            z: self.z * o.z,
        }
    }

    /// Branch-light orthonormal basis around a UNIT normal (Duff et al. 2017,
    /// "Building an Orthonormal Basis, Revisited"). Returns (tangent, bitangent)
    /// such that {tangent, bitangent, n} is right-handed and orthonormal.
    #[inline]
    pub fn build_onb(n: Vec3) -> (Vec3, Vec3) {
        let sign = if n.z >= 0.0 { 1.0 } else { -1.0 };
        let a = -1.0 / (sign + n.z);
        let b = n.x * n.y * a;
        let tangent = Vec3 {
            x: 1.0 + sign * n.x * n.x * a,
            y: sign * b,
            z: -sign * n.x,
        };
        let bitangent = Vec3 {
            x: b,
            y: sign + n.y * n.y * a,
            z: -n.y,
        };
        (tangent, bitangent)
    }
}

impl Add for Vec3 {
    type Output = Vec3;
    #[inline]
    fn add(self, o: Vec3) -> Vec3 {
        Vec3 { x: self.x + o.x, y: self.y + o.y, z: self.z + o.z }
    }
}
impl Sub for Vec3 {
    type Output = Vec3;
    #[inline]
    fn sub(self, o: Vec3) -> Vec3 {
        Vec3 { x: self.x - o.x, y: self.y - o.y, z: self.z - o.z }
    }
}
impl Mul<f32> for Vec3 {
    type Output = Vec3;
    #[inline]
    fn mul(self, s: f32) -> Vec3 {
        Vec3 { x: self.x * s, y: self.y * s, z: self.z * s }
    }
}
impl Div<f32> for Vec3 {
    type Output = Vec3;
    #[inline]
    fn div(self, s: f32) -> Vec3 {
        let inv = 1.0 / s;
        Vec3 { x: self.x * inv, y: self.y * inv, z: self.z * inv }
    }
}
impl Neg for Vec3 {
    type Output = Vec3;
    #[inline]
    fn neg(self) -> Vec3 {
        Vec3 { x: -self.x, y: -self.y, z: -self.z }
    }
}
