import { SearchIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { useLibraryItems } from '@/api/hooks/library'
import type { Schemas } from '@/api/client'
import { AuthedPoster } from '@/components/AuthedPoster'
import { LibrarySummaryCards } from '@/components/LibrarySummaryCards'
import { LibraryViewSwitch } from '@/components/LibraryViewSwitch'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useRingLoader } from '@/components/RingLoader'
import { t } from '@/lib/i18n'
import { DUPLICATES_STATUS, type StateSummary, type StatusOption } from '@/lib/library-filters'
import { STATUS_STYLES } from '@/lib/status-styles'
import { cn } from '@/lib/utils'
import { ItemDetailSheet, type OpenItem } from '@/pages/library/ItemDetailSheet'

type MediaItemOverview = Schemas['MediaItemOverview']

const PAGE_SIZE = 40

// Una card per film, una per SERIE (non per episodio: a parità di
// poster sarebbero migliaia di card identiche).
interface PosterCard {
  key: string
  content_type: string
  tmdb_id: number
  title: string | null
  year: number | null
  has_poster: boolean
  episodes: number
  sizeBytes: number
  counts: Record<StatusKey, number>
}

// Pallini di stato, contati per file (per una serie: per episodio). I
// file esclusi non contano mai, come nel resto dell'interfaccia.
type StatusKey = 'seeding' | 'orphan' | 'review' | 'duplicate'

const STATUS_DOTS: { key: StatusKey; className: string }[] = (
  ['seeding', 'orphan', 'review', 'duplicate'] as const
).map((key) => ({ key, className: STATUS_STYLES[key].dot }))

function StatusDots({ counts, showCounts }: { counts: Record<StatusKey, number>; showCounts: boolean }) {
  return (
    <div className="flex items-center gap-2">
      {STATUS_DOTS.filter((d) => counts[d.key] > 0).map((d) => (
        <span
          key={d.key}
          className="flex items-center gap-1 text-[10px] text-white/80 tabular-nums"
          title={t(`library.status.${d.key}`)}
        >
          <span className={cn('size-2 rounded-full', d.className)} />
          {showCounts && counts[d.key]}
        </span>
      ))}
    </div>
  )
}

export function toCards(items: MediaItemOverview[]): PosterCard[] {
  const cards = new Map<string, PosterCard>()
  for (const item of items) {
    const key = `${item.content_type}-${item.tmdb_id}`
    const card = cards.get(key) ?? {
      key, content_type: item.content_type, tmdb_id: item.tmdb_id, title: null, year: null,
      has_poster: false, episodes: 0, sizeBytes: 0, counts: { seeding: 0, orphan: 0, review: 0, duplicate: 0 },
    }
    card.title ??= item.title ?? null
    card.year ??= item.year ?? null
    card.has_poster ||= item.has_poster
    card.episodes += 1
    for (const f of item.files) {
      if (f.excluded) continue
      card.sizeBytes += f.size_bytes ?? 0
      if (f.state === 'seeding') card.counts.seeding += 1
      else card.counts.orphan += 1
      if (f.in_review) card.counts.review += 1
      if (f.duplicate) card.counts.duplicate += 1
    }
    cards.set(key, card)
  }
  // Un titolo con soli file esclusi (es. un sample o un extra identificato
  // come film) è fuori da ogni controllo come i suoi file: senza questo
  // contava nel Total senza essere né seeding né orfano.
  return [...cards.values()].filter((c) => c.counts.seeding + c.counts.orphan > 0).sort((a, b) =>
    (a.title ?? '\uffff').localeCompare(b.title ?? '\uffff') || a.tmdb_id - b.tmdb_id,
  )
}

// Stessi filtri della vista folder, ma per titolo (film o serie intera).
const STATUS_OPTIONS: StatusOption[] = [
  { value: 'all', label: t('library.stateAll') },
  { value: 'seeding', label: t('library.stateSeeding') },
  { value: 'orphan_media', label: t('library.stateOrphanMedia') },
  { value: 'review', label: t('library.stateReview') },
  { value: DUPLICATES_STATUS, label: t('library.stateDuplicates') },
]

// "Seeding" = tutto in seed; "Orphaned" = almeno un file orfano (per una
// serie: almeno un episodio), e così via.
function matchesStatus(card: PosterCard, status: string): boolean {
  switch (status) {
    case 'seeding':
      return card.counts.seeding > 0 && card.counts.orphan === 0
    case 'orphan_media':
      return card.counts.orphan > 0
    case 'review':
      return card.counts.review > 0
    case DUPLICATES_STATUS:
      return card.counts.duplicate > 0
    default:
      return true
  }
}

function summarize(cards: PosterCard[]): Record<string, StateSummary> {
  const summary: Record<string, StateSummary> = {}
  for (const option of STATUS_OPTIONS) {
    const matching = cards.filter((c) => matchesStatus(c, option.value))
    summary[option.value] = { count: matching.length, size: matching.reduce((n, c) => n + c.sizeBytes, 0) }
  }
  return summary
}

function cardLabel(card: PosterCard): string {
  if (!card.title) return `${card.content_type === 'tv' ? 'TV' : t('library.movie')} #${card.tmdb_id}`
  return card.year ? `${card.title} (${card.year})` : card.title
}

// Titolo e pallini dentro il poster, in basso, su una sfumatura scura
// per restare leggibili su qualunque immagine. Il clic apre la scheda di
// dettaglio (TMDB è uno dei link lì dentro).
function GridCard({ card, onOpen }: { card: PosterCard; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} className="group text-left" title={cardLabel(card)}>
      <div className="relative aspect-[2/3] overflow-hidden rounded-md border transition group-hover:ring-2 group-hover:ring-primary/50">
        <AuthedPoster
          contentType={card.content_type}
          tmdbId={card.tmdb_id}
          hasPoster={card.has_poster}
          className="h-full w-full"
        />
        <div className="absolute inset-x-0 bottom-0 grid gap-1 bg-gradient-to-t from-black/90 via-black/60 to-transparent px-2 pt-8 pb-1.5">
          <p className="line-clamp-2 text-xs font-medium text-white">{cardLabel(card)}</p>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] text-white/70">
              {card.content_type === 'tv' && t('library.episodesCount', { count: card.episodes })}
            </span>
            <StatusDots counts={card.counts} showCounts={card.content_type === 'tv'} />
          </div>
        </div>
      </div>
    </button>
  )
}

export function PosterView() {
  const query = useLibraryItems()
  const { data } = query
  const loader = useRingLoader(query)
  const [openItem, setOpenItem] = useState<OpenItem | null>(null)
  const [contentType, setContentType] = useState<'movie' | 'tv'>('movie')
  const [status, setStatus] = useState('all')
  const [search, setSearch] = useState('')

  const allCards = useMemo(
    () => toCards((data ?? []).filter((item) => item.content_type === contentType)),
    [data, contentType],
  )
  const summary = useMemo(() => summarize(allCards), [allCards])
  const cards = useMemo(() => {
    const query = search.trim().toLowerCase()
    return allCards.filter(
      (card) =>
        matchesStatus(card, status) &&
        (!query || cardLabel(card).toLowerCase().includes(query)),
    )
  }, [allCards, status, search])
  // Solo i primi PAGE_SIZE poster, altri PAGE_SIZE ogni volta che il fondo
  // della griglia si avvicina: renderizzare centinaia di card (e i loro
  // poster) tutte insieme rallentava l'apertura della pagina.
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const node = sentinelRef.current
    if (!node || visibleCount >= cards.length) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setVisibleCount((n) => n + PAGE_SIZE)
      },
      { rootMargin: '600px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [visibleCount, cards.length])

  if (loader) return loader

  return (
    // Una colonna larga quanto lo schermo: niente figli che allargano la pagina.
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <LibraryViewSwitch />
        <Tabs
          value={contentType}
          onValueChange={(v) => {
            setContentType(v as 'movie' | 'tv')
            setVisibleCount(PAGE_SIZE)
          }}
        >
          <TabsList>
            <TabsTrigger value="movie">{t('library.movie')}</TabsTrigger>
            <TabsTrigger value="tv">TV</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>
      <LibrarySummaryCards
        statusOptions={STATUS_OPTIONS}
        summary={summary}
        activeStatus={status}
        onSelect={(value) => {
          setStatus(value)
          setVisibleCount(PAGE_SIZE)
        }}
      />
      <div className="flex flex-wrap items-center gap-3">
        {/* Come nella vista a cartelle: sotto sm filtrano le card, le tab scorrono. */}
        <Tabs
          value={status}
          onValueChange={(v) => {
            setStatus(v as string)
            setVisibleCount(PAGE_SIZE)
          }}
          className="hidden max-w-full min-w-0 sm:flex"
        >
          <TabsList className="scroll-strip max-w-full justify-start">
            {STATUS_OPTIONS.map((option) => (
              <TabsTrigger key={option.value} value={option.value}>
                {option.label} ({summary[option.value]?.count ?? 0})
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <div className="relative min-w-56 flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setVisibleCount(PAGE_SIZE)
            }}
            placeholder={t('library.searchTitlePlaceholder')}
            className="pl-8"
          />
        </div>
      </div>
      {cards.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {allCards.length === 0 ? t('library.noResolvedContent') : t('library.noFilesMatchFilters')}
        </p>
      ) : (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 sm:gap-4 md:grid-cols-6 lg:grid-cols-8">
          {cards.slice(0, visibleCount).map((card) => (
            <GridCard
              key={card.key}
              card={card}
              onOpen={() => setOpenItem({ contentType: card.content_type, tmdbId: card.tmdb_id })}
            />
          ))}
          {visibleCount < cards.length && <div ref={sentinelRef} className="col-span-full h-px" />}
        </div>
      )}
      <ItemDetailSheet item={openItem} onClose={() => setOpenItem(null)} />
    </div>
  )
}
