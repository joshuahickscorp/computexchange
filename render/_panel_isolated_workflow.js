export const meta = {
  name: 'forensic-photo-panel-isolated',
  description: 'Isolated single-image panel: each image judged by its own agent with NO other images visible, to test cross-batch contamination',
  phases: [{ title: 'Isolated', detail: 'one agent per image, zero batch context' }],
}

// args: { items: [{name, kind, label, path}], loop: N }
const A = typeof args === 'string' ? JSON.parse(args) : args
const ITEMS = A.items
const LOOP = A.loop

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    call: { type: 'string', enum: ['PHOTOGRAPH', 'CG_RENDER'] },
    confidence: { type: 'integer', description: '0-100 how sure of the call' },
    tells: { type: 'array', maxItems: 3, items: { type: 'string' } },
  },
  required: ['call', 'confidence', 'tells'],
}

const LENSES = [
  { name: 'reviewer', persona: 'a senior hardware reviewer who has personally photographed hundreds of mini-PCs, GPUs and dev kits for a major tech publication' },
  { name: 'lookdev', persona: 'a 3D render / lookdev artist who spots CG tells: shader response, light behaviour, geometry regularity, edge treatment' },
  { name: 'photographer', persona: 'a product photographer who knows exactly how real studio lighting, lens optics and camera sensors behave on metal and plastic' },
  { name: 'materials', persona: 'a materials and packaging specialist who knows how molded EVA/PE foam, anodized aluminium and bead-blasted metal physically look at close range' },
  { name: 'buyer', persona: 'a meticulous online buyer with no CG training who scrutinises listing photos for anything that feels off or too perfect to be a real photo' },
]

const prompt = (L, path) => `You are ${L.persona}.

Use the Read tool to open and view exactly ONE image file: ${path}

This image shows a single piece of computer hardware. It may be a genuine PHOTOGRAPH or a computer-generated (CG) RENDER. You have no other images to compare it against, and no information about what else exists in any batch or series - judge this ONE image entirely on its own visual evidence.

Return: call (PHOTOGRAPH or CG_RENDER), confidence (0-100), and up to 3 specific visual tells that drove your call. Use the structured output tool.`

phase('Isolated')
const results = await pipeline(
  ITEMS,
  (item) => parallel(LENSES.map((L) => () =>
    agent(prompt(L, item.path), { label: `iso:${item.label}:${L.name}`, phase: 'Isolated', schema: SCHEMA, agentType: 'general-purpose' })
      .then((r) => ({ lens: L.name, ...r }))
  )),
)

const byItem = ITEMS.map((item, i) => ({
  name: item.name, kind: item.kind, label: item.label,
  verdicts: (results[i] || []).filter(Boolean),
}))

return { loop: LOOP, items: byItem }
