export const meta = {
  name: 'geometry-deep-audit',
  description: 'Per-reference geometry audit of Studio + Spark renders vs real photos, graded /10',
  phases: [{ title: 'Audit', detail: 'one vision agent per reference cluster, geometry only' }],
}

const A = typeof args === 'string' ? JSON.parse(args) : args
const UNITS = A.units

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          feature: { type: 'string', description: 'the geometric feature, named precisely' },
          status: { type: 'string', enum: ['correct', 'wrong_shape', 'wrong_proportion', 'wrong_position', 'missing', 'extra', 'unverifiable'] },
          severity: { type: 'integer', description: '1 trivial to 5 dominant' },
          evidence: { type: 'string', description: 'what you SAW in ref vs render, with pixel-ratio numbers where possible' },
          fix_hint: { type: 'string', description: 'concrete geometric change (dimension, radius, position, count)' },
        },
        required: ['feature', 'status', 'severity', 'evidence', 'fix_hint'],
      },
    },
    grades: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          aspect: { type: 'string' },
          score: { type: 'integer', description: '0-10, 10 = geometrically indistinguishable from the reference' },
          why: { type: 'string' },
        },
        required: ['aspect', 'score', 'why'],
      },
    },
    notes: { type: 'string', description: 'reference-quality caveats, angles we lack, anything else' },
  },
  required: ['findings', 'grades', 'notes'],
}

const prompt = (u) => `You are a senior industrial-design QA inspector doing a GEOMETRY-ONLY audit of a 3D render against real product photographs. You are exacting: shape, proportion, position, radius, count, presence. You do NOT judge lighting, color, or material tone (those are locked elsewhere) except where a geometric error causes them.

REFERENCE photos (the real device · ground truth):
${u.refs.map((r) => '- ' + r).join('\n')}

RENDER frames (our current model):
${u.renders.length ? u.renders.map((r) => '- ' + r).join('\n') : '- NONE · this unit CATALOGS the reference features as a build spec for an unmodeled face'}

FOCUS: ${u.focus}

Method, strictly:
1. Read EVERY file listed, with the Read tool. Study the references first, then the renders.
2. Catalog every geometric feature visible in the references relevant to the focus · edges, radii, cutouts, insets, seams, feet, vents, ports, reliefs, proportions. For each: is it in the render, and is its shape/proportion/position right? Estimate ratios by pixel measurement (e.g. "slot width is ~0.09 of body width in ref, ~0.13 in render").
3. Severity: 5 = defines the object's read, 1 = only visible at macro. Be harsh · this audit feeds a fix loop, false "correct" costs more than false "wrong".
4. fix_hint must be actionable geometry: a dimension, a radius, a position in mm-relative terms, a count.
5. Grade each aspect listed in your focus 0-10 (10 = a machinist could not tell render from reference geometry). Independent judgment · do not inflate.

Return via the structured output tool only.`

phase('Audit')
const results = await parallel(UNITS.map((u) => () =>
  agent(prompt(u), { label: `audit:${u.key}`, phase: 'Audit', schema: SCHEMA, agentType: 'general-purpose' })
    .then((r) => ({ key: u.key, ...r }))
))

return { audits: results.filter(Boolean) }
