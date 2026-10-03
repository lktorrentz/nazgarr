import { useId } from 'react'

import { cn } from '@/lib/utils'

// L'icona della scansione in corso: un occhio di fuoco che scruta il disco.
// Una sfera di lingue di fuoco che partono dal centro, fiamme che si
// allungano ai lati, qualche saetta, e la fessura scura col bordo
// incandescente che guarda da un lato all'altro. Solo SVG: le fiamme
// tremolano con un rumore animato (SMIL), il resto con il CSS di index.css
// (.fiery-eye-*). Con "riduci movimento" resta fermo, a fissare.

// SMIL non si ferma con il CSS di "riduci movimento": si toglie qui.
const STILL = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

const CX = 100
const CY = 60
const SQUASH = 0.88 // la sfera è un po' più alta che larga

// Numeri casuali con un seme fisso: l'occhio è sempre lo stesso.
function seeded(seed: number) {
  let a = seed
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const PALETTE = [
  ['#5a0700', 3], ['#7e0f00', 4], ['#a81a00', 4], ['#cc2e00', 3], ['#e84d00', 3],
  ['#ff7310', 3], ['#ff9c30', 2], ['#ffc560', 1],
].flatMap(([color, weight]) => Array<string>(weight as number).fill(color as string))

type Tongue = { d: string; fill: string; opacity: number }

// Le lingue di fuoco: larghe alla base vicino alla pupilla, appuntite e un
// po' piegate verso fuori.
const TONGUES: Tongue[] = (() => {
  const rand = seeded(21)
  const at = (r: number, a: number) =>
    `${(CX + r * Math.cos(a) * SQUASH).toFixed(1)} ${(CY + r * Math.sin(a)).toFixed(1)}`
  return Array.from({ length: 300 }, () => {
    const a = rand() * Math.PI * 2
    const r0 = 10 + rand() * 8
    const r1 = r0 + 16 + rand() * 26
    const half = 0.04 + rand() * 0.08
    const bend = (rand() - 0.5) * 0.5
    const mid = at((r0 + r1) / 2, a + bend / 2)
    return {
      d: `M${at(r0, a - half)} Q${mid} ${at(r1, a + bend)} Q${mid} ${at(r0, a + half)} Z`,
      fill: PALETTE[Math.floor(rand() * PALETTE.length)],
      opacity: 0.45 + rand() * 0.5,
    }
  })
})()

// Le saette: spezzate che partono dai lati della sfera.
const BOLTS: string[] = (() => {
  const rand = seeded(5)
  return [[CX - 26, CY - 6, -1, 10], [CX + 26, CY + 5, 1, 11], [CX - 22, CY + 12, -1, 6]].map(([x0, y0, dir, n]) => {
    let x = x0
    let y = y0
    const points = [`${x},${y}`]
    for (let i = 0; i < n; i++) {
      x += dir * (3 + rand() * 3)
      y += (rand() - 0.5) * 7
      points.push(`${x.toFixed(1)},${y.toFixed(1)}`)
    }
    return points.join(' ')
  })
})()

const SLIT = (w: number, h: number) => `M${CX} ${CY - h} Q${CX + w} ${CY} ${CX} ${CY + h} Q${CX - w} ${CY} ${CX} ${CY - h} Z`

function Flicker({ values, dur }: { values: string; dur: string }) {
  if (STILL) return null
  return <animate attributeName="baseFrequency" dur={dur} repeatCount="indefinite" values={values} />
}

export function FieryEye({ className, title }: { className?: string; title?: string }) {
  const id = useId().replace(/:/g, '')
  const ref = (name: string) => `fe-${name}-${id}`
  const url = (name: string) => `url(#${ref(name)})`
  return (
    <svg
      viewBox="0 0 200 120"
      className={cn('fiery-eye overflow-visible', className)}
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <defs>
        <radialGradient id={ref('halo')} cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#ff4a00" stopOpacity="0.65" />
          <stop offset="0.5" stopColor="#9a1200" stopOpacity="0.28" />
          <stop offset="1" stopColor="#4a0000" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={ref('core')} cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#2a0300" />
          <stop offset="0.35" stopColor="#7a1000" />
          <stop offset="0.65" stopColor="#c52a00" stopOpacity="0.85" />
          <stop offset="1" stopColor="#ff5a00" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={ref('dark')} cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#140000" stopOpacity="0.95" />
          <stop offset="0.65" stopColor="#2a0200" stopOpacity="0.55" />
          <stop offset="1" stopColor="#2a0200" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={ref('wisp')} x1="0" x2="1">
          <stop offset="0" stopColor="#ff3d00" stopOpacity="0" />
          <stop offset="0.5" stopColor="#ff8a1c" />
          <stop offset="1" stopColor="#ff3d00" stopOpacity="0" />
        </linearGradient>
        {/* Le lingue di fuoco, mosse da un rumore e sfumate in un bagliore. */}
        <filter id={ref('flame')} x="-40%" y="-40%" width="180%" height="180%">
          <feTurbulence type="fractalNoise" baseFrequency="0.06" numOctaves="3" seed="4" result="noise">
            <Flicker dur="2.2s" values="0.06;0.075;0.055;0.06" />
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="6" xChannelSelector="R" yChannelSelector="G" result="moved" />
          <feGaussianBlur in="moved" stdDeviation="1.6" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="moved" />
          </feMerge>
        </filter>
        <filter id={ref('edge')} x="-40%" y="-40%" width="180%" height="180%">
          <feTurbulence type="fractalNoise" baseFrequency="0.07" numOctaves="3" seed="5" result="noise">
            <Flicker dur="1.7s" values="0.07;0.09;0.065;0.07" />
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="10" xChannelSelector="R" yChannelSelector="G" />
          <feGaussianBlur stdDeviation="1.2" />
        </filter>
        {/* Le fiamme ai lati: stirate in orizzontale, più veloci. */}
        <filter id={ref('wild')} x="-30%" y="-150%" width="160%" height="400%">
          <feTurbulence type="fractalNoise" baseFrequency="0.03 0.2" numOctaves="3" seed="9" result="noise">
            <Flicker dur="1.2s" values="0.03 0.2;0.04 0.27;0.025 0.17;0.03 0.2" />
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="14" xChannelSelector="R" yChannelSelector="G" />
          <feGaussianBlur stdDeviation="1.1" />
        </filter>
        <filter id={ref('glow')} x="-100%" y="-50%" width="300%" height="200%">
          <feGaussianBlur stdDeviation="1.6" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <filter id={ref('soft')}>
          <feGaussianBlur stdDeviation="0.7" />
        </filter>
      </defs>
      <ellipse className="fiery-eye-glow" cx={CX} cy={CY} rx="98" ry="58" fill={url('halo')} />
      <g filter={url('wild')} opacity="0.8">
        <ellipse cx={CX - 42} cy={CY - 3} rx="46" ry="6" fill={url('wisp')} />
        <ellipse cx={CX + 42} cy={CY + 4} rx="48" ry="5" fill={url('wisp')} />
        <ellipse cx={CX - 30} cy={CY + 15} rx="28" ry="3.5" fill={url('wisp')} opacity="0.6" />
        <ellipse cx={CX + 28} cy={CY - 15} rx="30" ry="3.5" fill={url('wisp')} opacity="0.6" />
      </g>
      <ellipse cx={CX} cy={CY} rx="46" ry="52" fill={url('core')} filter={url('edge')} />
      <g filter={url('flame')}>
        {TONGUES.map((tongue, i) => (
          <path key={i} d={tongue.d} fill={tongue.fill} opacity={tongue.opacity} />
        ))}
      </g>
      <g stroke="#ffeeb0" strokeWidth="0.55" fill="none" filter={url('glow')}>
        {BOLTS.map((points, i) => (
          <polyline key={i} className={`fiery-eye-bolt fiery-eye-bolt-${i}`} points={points} />
        ))}
      </g>
      <g className="fiery-eye-pupil">
        <ellipse cx={CX} cy={CY} rx="15" ry="32" fill={url('dark')} filter={url('soft')} />
        <path d={SLIT(12, 31)} fill="none" stroke="#ffcf40" strokeWidth="1.8" filter={url('glow')} />
        <path d={SLIT(10.5, 30)} fill="#070000" filter={url('soft')} />
      </g>
    </svg>
  )
}
