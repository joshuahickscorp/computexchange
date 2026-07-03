export const meta = {
  name: 'forensic-photo-panel',
  description: 'One forensic panel loop: 5 cold vision agents classify each image photo-vs-render',
  phases: [{ title: 'Panel', detail: '5 diverse cold agents, forced choice per image' }],
}

// args: { dir: absolute neutral folder, images: ["img_01.png",...], loop: N }
const A = typeof args === 'string' ? JSON.parse(args) : args
const DIR = A.dir
const IMAGES = A.images
const LOOP = A.loop

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          image: { type: 'string', description: 'file name, e.g. img_03.png' },
          call: { type: 'string', enum: ['PHOTOGRAPH', 'CG_RENDER'] },
          confidence: { type: 'integer', description: '0-100 how sure of the call' },
          tells: {
            type: 'array', maxItems: 3, items: { type: 'string' },
            description: 'specific visual observations that drove the call',
          },
        },
        required: ['image', 'call', 'confidence', 'tells'],
      },
    },
  },
  required: ['verdicts'],
}

const LENSES = [
  { name: 'reviewer', persona: 'a senior hardware reviewer who has personally photographed hundreds of mini-PCs, GPUs and dev kits for a major tech publication' },
  { name: 'lookdev', persona: 'a 3D render / lookdev artist who spots CG tells: shader response, light behaviour, geometry regularity, edge treatment' },
  { name: 'photographer', persona: 'a product photographer who knows exactly how real studio lighting, lens optics (DOF, aberration, bloom) and camera sensors behave on metal and plastic' },
  { name: 'materials', persona: 'a materials and packaging specialist who knows how molded EVA/PE foam, anodized aluminium and bead-blasted metal physically look at close range' },
  { name: 'buyer', persona: 'a meticulous online buyer with no CG training who scrutinises listing photos for anything that feels off or too perfect to be a real photo' },
]

const prompt = (L) => `You are ${L.persona}.

In the folder ${DIR} there are ${IMAGES.length} image files:
${IMAGES.join(', ')}

Each shows computer hardware. Some may be genuine PHOTOGRAPHS; some may be computer-generated (CG) RENDERS. You do NOT know the mix - it could be all photographs, all renders, or any blend. Do not assume any balance.

For EVERY image file, use the Read tool to open and actually VIEW it (read all ${IMAGES.length}), then judge each one strictly on its own merits:
- call: PHOTOGRAPH or CG_RENDER
- confidence: 0-100
- tells: up to 3 SPECIFIC visual observations that drove your call. If you call CG_RENDER, name what gives it away. If you call PHOTOGRAPH, name what convinced you it is a real photo.

Judge from your ${L.name} expertise. View every image before answering. Return exactly one verdict per image via the structured output tool.`

phase('Panel')
const results = await parallel(LENSES.map((L) => () =>
  agent(prompt(L), { label: `panel:${L.name}`, phase: 'Panel', schema: SCHEMA, agentType: 'general-purpose' })
    .then((r) => ({ lens: L.name, verdicts: (r && r.verdicts) || [] }))
))

return { loop: LOOP, panel: results.filter(Boolean) }
