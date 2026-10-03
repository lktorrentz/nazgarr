import { useId } from 'react'

import { cn } from '@/lib/utils'

// L'icona della scansione in corso: un occhio infuocato che scruta il disco.
// Un'iride a mandorla con le fiamme che tremolano (turbolenza animata), uno
// strato di fiamme più mosse attorno, la pupilla a fessura che va da un lato
// all'altro e un alone che pulsa. Solo SVG e CSS (le animazioni CSS stanno in
// index.css, .fiery-eye-*): con "riduci movimento" resta fermo, a fissare.

// Le fiamme sono animazioni SVG (SMIL), che il CSS di "riduci movimento" non
// ferma: si tolgono qui.
const STILL = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

const ALMOND = 'M3 20 Q32 -2 61 20 Q32 42 3 20 Z'
const OUTER = 'M-2 20 Q32 -10 66 20 Q32 50 -2 20 Z'

function Flicker({ values, dur }: { values: string; dur: string }) {
  if (STILL) return null
  return <animate attributeName="baseFrequency" dur={dur} repeatCount="indefinite" values={values} />
}

export function FieryEye({ className, title }: { className?: string; title?: string }) {
  const id = useId().replace(/:/g, '')
  const ids = {
    iris: `fe-iris-${id}`, outer: `fe-outer-${id}`, glow: `fe-glow-${id}`, pupil: `fe-pupil-${id}`,
    flames: `fe-flames-${id}`, wild: `fe-wild-${id}`, clip: `fe-clip-${id}`,
  }
  return (
    <svg
      viewBox="0 0 64 40"
      className={cn('fiery-eye overflow-visible', className)}
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <defs>
        <radialGradient id={ids.iris} cx="50%" cy="50%" r="55%">
          <stop offset="0%" stopColor="#fff7c2" />
          <stop offset="22%" stopColor="#ffd23f" />
          <stop offset="55%" stopColor="#ff7a00" />
          <stop offset="85%" stopColor="#c81e00" />
          <stop offset="100%" stopColor="#4a0500" />
        </radialGradient>
        <radialGradient id={ids.outer} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ffb300" />
          <stop offset="60%" stopColor="#ff4500" />
          <stop offset="100%" stopColor="#8b0000" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={ids.glow} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ff8a00" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#ff3d00" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={ids.pupil} x1="0" x2="1">
          <stop offset="0" stopColor="#120000" stopOpacity="0" />
          <stop offset="0.35" stopColor="#120000" stopOpacity="0.5" />
          <stop offset="0.5" stopColor="#0a0000" />
          <stop offset="0.65" stopColor="#120000" stopOpacity="0.5" />
          <stop offset="1" stopColor="#120000" stopOpacity="0" />
        </linearGradient>
        {/* Il bordo dell'iride, deformato da un rumore che cambia. */}
        <filter id={ids.flames} x="-30%" y="-60%" width="160%" height="220%">
          <feTurbulence type="fractalNoise" baseFrequency="0.09 0.16" numOctaves="2" seed="3" result="noise">
            <Flicker dur="1.6s" values="0.09 0.16;0.11 0.2;0.08 0.14;0.09 0.16" />
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="4" xChannelSelector="R" yChannelSelector="G" />
        </filter>
        {/* Le fiamme attorno: più mosse e più veloci. */}
        <filter id={ids.wild} x="-30%" y="-80%" width="160%" height="260%">
          <feTurbulence type="fractalNoise" baseFrequency="0.06 0.22" numOctaves="3" seed="8" result="noise">
            <Flicker dur="1.1s" values="0.06 0.22;0.08 0.3;0.05 0.18;0.06 0.22" />
          </feTurbulence>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="12" xChannelSelector="R" yChannelSelector="G" />
          <feGaussianBlur stdDeviation="0.6" />
        </filter>
        <clipPath id={ids.clip}>
          <path d={ALMOND} />
        </clipPath>
      </defs>
      <ellipse className="fiery-eye-glow" cx="32" cy="20" rx="36" ry="22" fill={`url(#${ids.glow})`} />
      <path d={OUTER} fill={`url(#${ids.outer})`} filter={`url(#${ids.wild})`} opacity="0.85" />
      <path d={ALMOND} fill={`url(#${ids.iris})`} filter={`url(#${ids.flames})`} />
      <g clipPath={`url(#${ids.clip})`}>
        <g className="fiery-eye-pupil">
          <path d="M32 9 Q38 20 32 31 Q26 20 32 9 Z" fill={`url(#${ids.pupil})`} />
          <path d="M32 9.5 Q34.6 20 32 30.5 Q29.4 20 32 9.5 Z" fill="#0a0000" />
        </g>
      </g>
    </svg>
  )
}
