import { CheckIcon, RotateCwIcon, SearchIcon, TriangleAlertIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import {
  posterUrl,
  useMetadataDetails,
  useMetadataSearch,
  type MetadataCandidate,
  type MetadataDetails,
} from '@/api/hooks/metadata'
import { useSetting } from '@/api/hooks/settings'
import {
  useConfirmMatch,
  useEpisodeOrders,
  useReidentify,
  useSplitUpload,
  type EpisodeOrder,
  type EpisodeOrders,
  type UploadJob,
} from '@/api/hooks/uploads'
import { AuthedPoster } from '@/components/AuthedPoster'
import { ChoiceCards } from '@/components/ChoiceCards'
import { InfoPopover } from '@/components/InfoPopover'
import { ForcedIdFields } from '@/components/upload/ForcedIdFields'
import { MetadataLinks } from '@/components/upload/MetadataLinks'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroupItem, ToggleGroupSingle } from '@/components/ui/toggle-group'
import { t } from '@/lib/i18n'
import { episodeLabel, seasonCounts, sourceProblems, translateEpisode } from '@/lib/episodeOrders'
import { fromForcedIds, missingEpisodes, toForcedIds, type UploadKind } from '@/lib/upload'
import { cn } from '@/lib/utils'
import { opensOnHover } from '@/lib/pointer'

interface Layout {
  kind: UploadKind
  videos: { relative_path: string; size_bytes: number; season: number | null; episodes: number[] }[]
  episodes_by_season: Record<string, number[]>
  seasons: number[]
}

const keyOf = (c: Pick<MetadataCandidate, 'content_type' | 'tmdb_id'>) => `${c.content_type}:${c.tmdb_id}`

function sourceLabel(source: string | undefined) {
  if (!source || source === 'search') return null
  return t(`upload.match.source.${source}`)
}

function CandidateCard({
  candidate,
  selected,
  onSelect,
}: {
  candidate: MetadataCandidate
  selected: boolean
  onSelect: () => void
}) {
  const label = sourceLabel(candidate.source)
  return (
    <button type="button" onClick={onSelect} className="group text-left" aria-pressed={selected}>
      <div
        className={cn(
          'relative aspect-[2/3] overflow-hidden rounded-md border transition',
          selected ? 'ring-2 ring-primary' : 'group-hover:ring-2 group-hover:ring-primary/50',
        )}
      >
        <AuthedPoster
          contentType={candidate.content_type}
          tmdbId={candidate.tmdb_id}
          hasPoster
          url={posterUrl(candidate)}
          className="h-full w-full"
        />
        <div className="absolute inset-x-1.5 top-1.5 flex items-start justify-between gap-1">
          <Badge className="bg-black/70 text-[10px] text-white">
            {candidate.content_type === 'tv' ? t('upload.match.series') : t('upload.match.movie')}
          </Badge>
          {selected && (
            <span className="flex size-5 items-center justify-center rounded-full bg-primary text-primary-foreground">
              <CheckIcon className="size-3.5" />
            </span>
          )}
        </div>
        <div className="absolute inset-x-0 bottom-0 grid gap-0.5 bg-gradient-to-t from-black/90 via-black/60 to-transparent px-2 pt-8 pb-1.5">
          <p className="line-clamp-2 text-xs font-medium text-white">{candidate.title ?? `#${candidate.tmdb_id}`}</p>
          {/* Su un poster stretto (telefono) anno, origine e percentuale vanno a capo invece di sovrapporsi. */}
          <p className="flex flex-wrap items-center justify-between gap-1 text-[10px] text-white/70">
            <span>{candidate.year ?? '—'}</span>
            {label && <span className="min-w-0 truncate rounded bg-white/15 px-1">{label}</span>}
            {/* Quanto è sicuro (nazgarr/upload/match_score.py): sopra la soglia la cartella osservata lo conferma da sola. */}
            {candidate.confidence != null && (
              <span
                className={cn('ml-auto rounded px-1 tabular-nums', candidate.ambiguous ? 'bg-amber-500/40' : 'bg-white/15')}
                title={candidate.ambiguous ? t('upload.match.ambiguous') : confidenceExplained(candidate)}
              >
                {Math.round(candidate.confidence * 100)}%
              </span>
            )}
          </p>
        </div>
      </div>
    </button>
  )
}

const percent = (value: number | undefined) => `${Math.round((value ?? 0) * 100)}%`

// Da cosa viene la confidence di un candidato (nazgarr/upload/match_score.py).
function confidenceExplained(candidate: MetadataCandidate): string {
  const parts = candidate.confidence_parts
  if (!parts) return t('upload.match.confidence')
  if (parts.basis !== 'name') return t(`upload.match.basis.${parts.basis}`)
  return t('upload.match.basis.name', { title: percent(parts.title), year: percent(parts.year), type: percent(parts.type) })
}

// Quanto è affidabile il candidato scelto e da cosa viene, contro la soglia
// del match automatico (Settings › Releases): per capire a che valore metterla.
// L'affidabilità del match in alto a destra, solo la percentuale (verde se
// supera la soglia del match automatico); passandoci sopra, il perché.
function ConfidenceBadge({ candidate }: { candidate: MetadataCandidate }) {
  const { data } = useSetting('upload_auto_match_threshold')
  if (candidate.confidence == null) return null
  const raw = data?.value
  const threshold = raw == null || raw === '' ? 0.9 : Number(raw)
  const off = !(threshold > 0 && threshold <= 1)
  const passes = !off && !candidate.ambiguous && candidate.confidence >= threshold
  const summary = off
    ? t('upload.match.summaryOff')
    : candidate.ambiguous
      ? t('upload.match.summaryAmbiguous')
      : t(passes ? 'upload.match.summaryAbove' : 'upload.match.summaryBelow', { threshold: percent(threshold) })
  return (
    <Popover>
      <PopoverTrigger
        openOnHover={opensOnHover()}
        delay={150}
        aria-label={t('upload.match.reliability', { confidence: percent(candidate.confidence) })}
        className={cn(
          'cursor-help rounded-md px-2 py-0.5 font-mono text-sm font-semibold tabular-nums',
          passes
            ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
            : off
              ? 'bg-muted text-muted-foreground'
              : 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
        )}
      >
        {percent(candidate.confidence)}
      </PopoverTrigger>
      <PopoverContent align="end" className="grid w-96 max-w-[calc(100vw-2rem)] gap-2 text-xs">
        <p className="font-medium">
          {t('upload.match.reliability', { confidence: percent(candidate.confidence) })}
          <span className={cn('ml-1.5 font-normal', passes ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground')}>
            {summary}
          </span>
        </p>
        <ConfidenceFactors candidate={candidate} />
      </PopoverContent>
    </Popover>
  )
}

// Riga per riga, cosa ha portato a quella percentuale: il titolo che
// somiglia di più, l'anno del file contro quello di TMDB, il tipo, e la
// penalità se un altro titolo è quasi pari.
function ConfidenceFactors({ candidate }: { candidate: MetadataCandidate }) {
  const parts = candidate.confidence_parts
  if (!parts) return null
  if (parts.basis !== 'name') return <p className="text-muted-foreground">{t(`upload.match.basis.${parts.basis}`)}</p>
  const kind = (type: string | undefined) => (type === 'tv' ? t('upload.match.series') : t('upload.match.movie'))
  const yearReason =
    parts.year_guess == null || parts.year_candidate == null
      ? t('upload.match.factor.yearUnknown')
      : parts.year_guess === parts.year_candidate
        ? t('upload.match.factor.yearSame')
        : Math.abs(parts.year_guess - parts.year_candidate) === 1
          ? t('upload.match.factor.yearOneOff')
          : t('upload.match.factor.yearDifferent')
  const rows: [string, number, string][] = [
    [t('upload.match.factor.title'), parts.title ?? 0,
      t('upload.match.factor.titleWhy', { guess: parts.title_guess ?? '—', matched: parts.title_matched ?? '—' })],
    [t('upload.match.factor.year'), parts.year ?? 0,
      t('upload.match.factor.yearWhy', { guess: parts.year_guess ?? '—', candidate: parts.year_candidate ?? '—', reason: yearReason })],
    [t('upload.match.factor.type'), parts.type ?? 0,
      parts.type === 1
        ? t('upload.match.factor.typeSame', { type: kind(parts.type_candidate) })
        : t('upload.match.factor.typeDifferent', { guess: kind(parts.type_guess), candidate: kind(parts.type_candidate) })],
  ]
  if (parts.ambiguous) rows.push([t('upload.match.factor.ambiguous'), parts.ambiguous, t('upload.match.factor.ambiguousWhy')])
  return (
    <div className="grid gap-1">
      {rows.map(([label, value, why]) => (
        <div key={label} className="grid grid-cols-[4.5rem_3rem_minmax(0,1fr)] gap-2">
          <span className="text-muted-foreground">{label}</span>
          <span className="text-right font-mono tabular-nums">{percent(value)}</span>
          <span className="min-w-0 break-words text-muted-foreground">{why}</span>
        </div>
      ))}
      <p className="text-muted-foreground">
        {t('upload.match.factor.product', {
          factors: rows.map(([, value]) => percent(value)).join(' × '),
          confidence: percent(candidate.confidence),
        })}
      </p>
    </div>
  )
}

function CandidateGrid({
  candidates,
  selectedKey,
  onSelect,
}: {
  candidates: MetadataCandidate[]
  selectedKey: string | null
  onSelect: (candidate: MetadataCandidate) => void
}) {
  // Due colonne sui telefoni stretti: a tre il titolo sul poster non si legge.
  return (
    <div className="grid grid-cols-2 gap-3 min-[400px]:grid-cols-3 sm:grid-cols-4 xl:grid-cols-5">
      {candidates.map((candidate) => (
        <CandidateCard
          key={keyOf(candidate)}
          candidate={candidate}
          selected={keyOf(candidate) === selectedKey}
          onSelect={() => onSelect(candidate)}
        />
      ))}
    </div>
  )
}

function DetailPanel({ candidate, details, isPending }: {
  candidate: MetadataCandidate
  details: MetadataDetails | undefined
  isPending: boolean
}) {
  const info = details ?? candidate
  return (
    <div className="grid gap-3">
      <div className="flex gap-3">
        <AuthedPoster
          contentType={candidate.content_type}
          tmdbId={candidate.tmdb_id}
          hasPoster
          url={posterUrl(candidate)}
          className="aspect-[2/3] w-20 shrink-0 overflow-hidden rounded"
        />
        <div className="grid min-w-0 content-start gap-1">
          <p className="font-medium leading-tight">
            {info.title} {info.year && <span className="text-muted-foreground">({info.year})</span>}
          </p>
          {info.original_title && info.original_title !== info.title && (
            <p className="text-xs text-muted-foreground italic">{info.original_title}</p>
          )}
          <div className="flex flex-wrap gap-1">
            <Badge variant="outline">
              {candidate.content_type === 'tv' ? t('upload.match.series') : t('upload.match.movie')}
            </Badge>
            {details?.genres.slice(0, 3).map((genre) => (
              <Badge key={genre} variant="secondary">
                {genre}
              </Badge>
            ))}
          </div>
          {details?.runtime && (
            <p className="text-xs text-muted-foreground">{t('upload.match.runtime', { minutes: details.runtime })}</p>
          )}
        </div>
      </div>
      {isPending && !details && <p className="text-xs text-muted-foreground">{t('common.loading')}</p>}
      {info.overview && <p className="line-clamp-2 text-xs leading-relaxed" title={info.overview}>{info.overview}</p>}
      {details && details.cast.length > 0 && (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{t('upload.match.cast')}:</span> {details.cast.join(', ')}
        </p>
      )}
      <MetadataLinks
        job={{
          content_type: candidate.content_type,
          tmdb_id: candidate.tmdb_id,
          imdb_id: details?.imdb_id,
          tvdb_id: details?.tvdb_id,
        }}
      />
    </div>
  )
}

function defaultKind(job: UploadJob, contentType: string): UploadKind {
  const detected = (job.kind ?? 'movie') as UploadKind
  if (contentType === 'movie') return 'movie'
  if (detected !== 'movie') return detected
  return job.is_dir ? 'season_pack' : 'episode'
}

interface SeasonRow {
  season_number: number
  name: string | null
  episode_count: number
  air_date: string | null
}

function SeasonPicker({
  rows: catalogRows,
  catalog,
  found,
  detected,
  kind,
  seasons,
  onSeasonsChange,
  episode,
  onEpisodeChange,
}: {
  rows: SeasonRow[]
  catalog: boolean // le stagioni vengono da TMDB o da un ordinamento: quelle trovate fuori vanno segnalate
  found: Record<string, number[]>
  detected: Set<number>
  kind: UploadKind
  seasons: number[]
  onSeasonsChange: (seasons: number[]) => void
  episode: number | null
  onEpisodeChange: (episode: number | null) => void
}) {
  // Le stagioni del catalogo, più quelle trovate nella sorgente che il
  // catalogo non conosce (numerazione diversa): si vedono comunque, con l'avviso.
  const known = new Set(catalogRows.map((s) => s.season_number))
  // La stagione 0 (Specials) c'è sempre, in fondo: anche quando la sorgente
  // non la dichiara (episodi speciali senza S00 nel nome).
  const rows = [
    ...catalogRows,
    ...[...detected].filter((n) => !known.has(n)).map((n) => ({ season_number: n, name: null, episode_count: 0, air_date: null })),
  ].sort((a, b) => Number(a.season_number === 0) - Number(b.season_number === 0) || a.season_number - b.season_number)
  const unknownDetected = catalog ? [...detected].filter((n) => !known.has(n)) : []
  const multiple = kind === 'complete_pack'

  const toggle = (n: number) => {
    if (!multiple) return onSeasonsChange([n])
    onSeasonsChange(seasons.includes(n) ? seasons.filter((s) => s !== n) : [...seasons, n].sort((a, b) => a - b))
  }

  return (
    <div className="grid gap-2">
      <Label>{multiple ? t('upload.match.seasonsLabel') : t('upload.match.seasonLabel')}</Label>
      {detected.size === 0 && (
        <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
          <TriangleAlertIcon className="size-3.5" />
          {t('upload.match.noSeasonDetected')}
        </p>
      )}
      {unknownDetected.length > 0 && (
        <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
          <TriangleAlertIcon className="size-3.5" />
          {t('upload.match.seasonUnknownToTmdb', { seasons: unknownDetected })}
        </p>
      )}
      <div className="grid max-h-64 gap-1 overflow-auto">
        {rows.map((season) => {
          const n = season.season_number
          const episodes = found[String(n)] ?? []
          const missing = season.episode_count ? missingEpisodes(episodes, season.episode_count) : []
          const active = seasons.includes(n)
          return (
            <button
              key={n}
              type="button"
              role={multiple ? 'checkbox' : 'radio'}
              aria-checked={active}
              onClick={() => toggle(n)}
              className={cn(
                // Su schermi stretti i conteggi vanno a capo; al tocco la riga è più alta.
                'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-2.5 py-1.5 text-left text-xs transition pointer-coarse:py-2.5',
                active ? 'border-primary bg-primary/10' : 'hover:bg-muted',
              )}
            >
              <span
                className={cn(
                  'flex size-3.5 shrink-0 items-center justify-center border',
                  multiple ? 'rounded-sm' : 'rounded-full',
                  active && 'border-primary bg-primary text-primary-foreground',
                )}
              >
                {active && <CheckIcon className="size-2.5" />}
              </span>
              <span className="font-medium">{n === 0 ? t('upload.match.specials') : t('upload.match.seasonN', { n })}</span>
              {detected.has(n) && <Badge variant="secondary" className="h-4 px-1 text-[10px]">{t('upload.match.detected')}</Badge>}
              <span className="ml-auto text-muted-foreground tabular-nums">
                {season.episode_count
                  ? t('upload.match.episodesFound', { found: episodes.length, expected: season.episode_count })
                  : t('upload.match.episodesFoundNoTotal', { found: episodes.length })}
              </span>
              {episodes.length > 0 && missing.length > 0 && kind !== 'episode' && (
                // Quali mancano: al passaggio del mouse o al tocco, non solo in un title.
                <InfoPopover align="end" className="text-amber-600 dark:text-amber-400" content={missing.map((e) => `E${e}`).join(' ')}>
                  {t('upload.match.missingCount', { count: missing.length })}
                </InfoPopover>
              )}
            </button>
          )
        })}
      </div>
      {kind === 'episode' && (
        <div className="grid max-w-40 gap-1.5">
          <Label htmlFor="match-episode">{t('upload.match.episodeLabel')}</Label>
          <Input
            id="match-episode"
            type="number"
            min={0}
            value={episode ?? ''}
            onChange={(e) => onEpisodeChange(e.target.value === '' ? null : Number(e.target.value))}
          />
        </div>
      )}
    </div>
  )
}

// L'ordinamento degli episodi (nazgarr/library/episode_orders.py): proposto quello
// preferito per la serie, poi TVDB aired; se i file ne seguono un altro, un
// avviso con la scorciatoia per passarci, mai una scelta al posto dell'utente.
// Sotto, la corrispondenza fra i file e gli episodi dell'ordinamento scelto.
function EpisodeOrderPicker({ data, active, onChange }: {
  data: EpisodeOrders
  active: EpisodeOrder
  onChange: (key: string) => void
}) {
  const filesOrder = data.orders.find((o) => o.key === data.files_order)
  // L'avviso: i file non seguono TVDB aired (l'ordine di Sonarr); la scorciatoia porta lì.
  const tvdb = data.warning ? data.orders.find((o) => o.key === data.warning!.tvdb) : undefined
  const fitting = data.warning ? data.orders.find((o) => o.key === data.warning!.order) : undefined
  const found = data.found[data.files_order ?? ''] ?? {}
  const mapping = filesOrder
    ? Object.entries(found).flatMap(([season, eps]) =>
        eps.map((e) => ({ from: [Number(season), e] as const, to: translateEpisode(filesOrder, active, Number(season), e) })))
    : []
  const fitOf = (key: string) => data.fits[key]
  return (
    <div className="grid gap-2">
      <Label>{t('upload.match.orderLabel')}</Label>
      <Select value={active.key} onValueChange={(value) => value && onChange(String(value))}>
        <SelectTrigger className="w-full">
          <SelectValue>{() => active.label}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {data.orders.map((order) => {
            const fit = fitOf(order.key)
            return (
              <SelectItem key={order.key} value={order.key}>
                <span className="grid w-full">
                  <span className="flex items-center justify-between gap-3">
                    <span>{order.label}</span>
                    {fit && fit.files > 0 && (
                      <span className="text-xs text-muted-foreground">{t('metadata.orderFit', { score: `${Math.round(fit.score * 100)}%` })}</span>
                    )}
                  </span>
                  <span className="text-xs text-muted-foreground">{seasonCounts(order)}</span>
                </span>
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
      {sourceProblems(data.sources, t).map((line) => (
        <p key={line} className="text-xs text-muted-foreground">{line}</p>
      ))}
      {tvdb && fitting && active.key !== tvdb.key && (
        <div className="flex flex-wrap items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
          <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
          <span className="min-w-0 flex-1">{t('upload.match.orderWarning', { tvdb: tvdb.label, order: fitting.label })}</span>
          <button type="button" className="shrink-0 font-medium underline" onClick={() => onChange(tvdb.key)}>
            {t('upload.match.orderUse', { order: tvdb.label })}
          </button>
        </div>
      )}
      {mapping.length > 0 && filesOrder && filesOrder.key !== active.key && (
        <Collapsible>
          <CollapsibleTrigger className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
            {t('upload.match.orderMapping', { count: mapping.length })}
          </CollapsibleTrigger>
          <CollapsibleContent className="grid max-h-56 gap-0.5 overflow-auto pt-2 font-mono text-xs">
            {mapping.map(({ from, to }) => (
              <span key={`${from[0]}x${from[1]}`} className="flex gap-2">
                <span className="text-muted-foreground">{episodeLabel(from[0], [from[1]])}</span>
                <span>→</span>
                {to.length === 0 ? (
                  <span className="text-amber-600 dark:text-amber-400">{t('upload.match.orderNoMatch')}</span>
                ) : (
                  <span className="min-w-0 truncate">
                    {episodeLabel(to[0].season, to.filter((m) => m.season === to[0].season).map((m) => m.episode))}{' '}
                    <span className="font-sans text-muted-foreground">{to.flatMap((m) => m.titles).filter(Boolean).join(' + ')}</span>
                  </span>
                )}
              </span>
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}

export function MatchStep({ job }: { job: UploadJob }) {
  const candidates = job.candidates as unknown as MetadataCandidate[]
  const layout = job.layout as unknown as Layout | null
  const [selected, setSelected] = useState<MetadataCandidate | null>(candidates[0] ?? null)
  const [searchType, setSearchType] = useState<'movie' | 'tv'>((job.content_type as 'movie' | 'tv') ?? 'movie')
  const [searchDraft, setSearchDraft] = useState(job.title ?? '')
  const [searchQuery, setSearchQuery] = useState('')
  const search = useMetadataSearch(searchType, searchQuery, null)
  const details = useMetadataDetails(selected?.content_type ?? null, selected?.tmdb_id ?? null)
  const [kind, setKind] = useState<UploadKind>(() => defaultKind(job, selected?.content_type ?? 'movie'))
  const [seasonsDraft, setSeasons] = useState<number[] | null>(null)
  const [episodeDraft, setEpisode] = useState<number | null | undefined>(undefined)
  const [orderDraft, setOrderDraft] = useState<string | null>(null)
  const orders = useEpisodeOrders(job.id, selected?.content_type === 'tv' ? selected.tmdb_id : null)
  const activeOrder = orders.data?.orders.find((o) => o.key === (orderDraft ?? orders.data?.recommended)) ?? null
  // Stagioni ed episodi nella numerazione dell'ordinamento scelto: quelli dei
  // file tradotti; senza ordinamenti, quelli dei file e le stagioni di TMDB.
  const found = activeOrder ? orders.data!.found[activeOrder.key] ?? {} : layout?.episodes_by_season ?? {}
  const detected = new Set(activeOrder ? Object.keys(found).map(Number) : job.seasons)
  const seasonRows: SeasonRow[] = activeOrder
    ? activeOrder.seasons.map((s) => ({ season_number: s.season_number, name: null, episode_count: s.episodes.length, air_date: null }))
    : details.data?.seasons ?? []
  const detectedSeasons = [...detected].sort((a, b) => a - b)
  const seasons = seasonsDraft ?? (activeOrder ? detectedSeasons : job.seasons)
  const firstFound = found[String(seasons[0])]?.[0]
  const episode = episodeDraft !== undefined ? episodeDraft : activeOrder && kind === 'episode' && firstFound != null ? firstFound : job.episode ?? null
  const [ids, setIds] = useState(() => fromForcedIds(job.forced_ids))
  const confirm = useConfirmMatch(job.id)
  const split = useSplitUpload(job.id)
  const reidentify = useReidentify(job.id)
  const navigate = useNavigate()

  const searchResults = useMemo(() => {
    const shown = new Set(candidates.map(keyOf))
    return (search.data ?? []).filter((c) => !shown.has(keyOf(c)))
  }, [search.data, candidates])

  function select(candidate: MetadataCandidate) {
    setSelected(candidate)
    setKind(defaultKind(job, candidate.content_type))
    setOrderDraft(null)
    setSeasons(null)
    setEpisode(undefined)
  }

  function changeOrder(key: string) {
    setOrderDraft(key)
    setSeasons(null) // le stagioni trovate, nella nuova numerazione
    setEpisode(undefined)
  }

  const isTv = selected?.content_type === 'tv'
  const kindChoices = [
    { value: 'episode' as const, title: t('upload.kind.episode'), description: t('upload.kind.episodeHelp') },
    { value: 'season_pack' as const, title: t('upload.kind.season_pack'), description: t('upload.kind.season_packHelp') },
    { value: 'complete_pack' as const, title: t('upload.kind.complete_pack'), description: t('upload.kind.complete_packHelp') },
  ].filter((choice) => (job.is_dir ? choice.value !== 'episode' || (layout?.videos.length ?? 0) <= 1 : choice.value === 'episode'))

  const invalid =
    selected === null ||
    (isTv && seasons.length === 0) ||
    (isTv && kind !== 'complete_pack' && seasons.length !== 1) ||
    (isTv && kind === 'episode' && episode === null)

  function matchBody() {
    return {
      content_type: selected!.content_type,
      tmdb_id: selected!.tmdb_id,
      kind: isTv ? kind : 'movie',
      seasons: isTv ? seasons : [],
      episode: isTv && kind === 'episode' ? episode : null,
      episode_order: isTv ? activeOrder?.key ?? null : null,
    }
  }

  function submit() {
    if (!selected || invalid) return
    confirm.mutate(matchBody(), {
      onError: (error) => toast.error(t('upload.match.confirmFailed', { message: error.message })),
    })
  }

  // Una stagione incompleta (es. 2 episodi di 8): si può dividere in un
  // upload per episodio (nazgarr/upload/split.py). Non un pack di file scelti a mano.
  const videos = layout?.videos.length ?? 0
  const expected = seasonRows.find((row) => row.season_number === seasons[0])?.episode_count ?? 0
  const foundInSeason = (found[String(seasons[0])] ?? []).length
  const canSplit = isTv && kind === 'season_pack' && seasons.length === 1 && !job.pack_name && videos > 1
    && expected > 0 && foundInSeason > 0 && foundInSeason < expected

  function splitIntoEpisodes() {
    if (!selected || invalid) return
    split.mutate(matchBody(), {
      onSuccess: (result) => {
        toast.success(t('upload.match.splitDone', { count: result.job_ids.length }))
        navigate('/upload')
      },
      onError: (error) => toast.error(t('upload.match.splitFailed', { message: error.message })),
    })
  }

  // Sotto lg la colonna di sinistra si scioglie (contents) e la scheda
  // "Selezionato" sale subito dopo i candidati: toccato un poster, dettagli e
  // Conferma restano a portata di dito; la ricerca manuale scende dopo.
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="grid content-start gap-4 max-lg:contents">
        <Card>
          <CardHeader>
            <CardTitle>{t('upload.match.title')}</CardTitle>
            <CardDescription>{t('upload.match.description')}</CardDescription>
            {/* Rifà la ricerca (es. dopo aver impostato la lingua del tracker),
                con gli ID forzati se ce ne sono. */}
            <CardAction>
              <Button
                variant="ghost"
                size="sm"
                disabled={reidentify.isPending}
                title={t('upload.match.identifyAgainHelp')}
                onClick={() => reidentify.mutate(toForcedIds(ids), { onError: (error) => toast.error(error.message) })}
              >
                <RotateCwIcon className="size-4" />
                {t('upload.match.identifyAgain')}
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {candidates.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t('upload.match.noCandidates')}</p>
            ) : (
              <CandidateGrid candidates={candidates} selectedKey={selected && keyOf(selected)} onSelect={select} />
            )}
          </CardContent>
        </Card>

        <Card className="max-lg:order-2">
          <CardHeader>
            <CardTitle className="text-base">{t('upload.match.searchTitle')}</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4">
            <form
              className="flex flex-wrap gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                setSearchQuery(searchDraft.trim())
              }}
            >
              <ToggleGroupSingle
                value={searchType}
                onValueChange={(value) => setSearchType(value as 'movie' | 'tv')}
                variant="outline"
              >
                <ToggleGroupItem value="movie">{t('upload.match.movie')}</ToggleGroupItem>
                <ToggleGroupItem value="tv">{t('upload.match.series')}</ToggleGroupItem>
              </ToggleGroupSingle>
              <Input
                className="min-w-48 flex-1"
                value={searchDraft}
                placeholder={t('upload.match.searchPlaceholder')}
                onChange={(e) => setSearchDraft(e.target.value)}
              />
              <Button type="submit" variant="outline" disabled={!searchDraft.trim()}>
                <SearchIcon className="size-4" />
                {t('upload.match.search')}
              </Button>
            </form>
            {search.isFetching && <p className="text-sm text-muted-foreground">{t('common.loading')}</p>}
            {search.isError && <p className="text-sm text-destructive">{search.error.message}</p>}
            {searchQuery && search.data && searchResults.length === 0 && (
              <p className="text-sm text-muted-foreground">{t('upload.match.noSearchResults')}</p>
            )}
            {searchResults.length > 0 && (
              <CandidateGrid candidates={searchResults} selectedKey={selected && keyOf(selected)} onSelect={select} />
            )}

            <Collapsible>
              <CollapsibleTrigger className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
                {t('upload.match.forceIds')}
              </CollapsibleTrigger>
              <CollapsibleContent className="grid gap-3 pt-3">
                <ForcedIdFields ids={ids} onChange={setIds} />
                <Button
                  variant="outline"
                  className="w-fit"
                  disabled={reidentify.isPending}
                  onClick={() =>
                    reidentify.mutate(toForcedIds(ids), {
                      onError: (error) => toast.error(error.message),
                    })
                  }
                >
                  {t('upload.match.identifyAgain')}
                </Button>
              </CollapsibleContent>
            </Collapsible>
          </CardContent>
        </Card>
      </div>

      <Card className="h-fit max-lg:order-1 lg:sticky lg:top-4">
        <CardHeader>
          <CardTitle className="text-base">{t('upload.match.selected')}</CardTitle>
          {selected && (
            <CardAction>
              <ConfidenceBadge candidate={selected} />
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="grid gap-4">
          {selected ? (
            <DetailPanel candidate={selected} details={details.data} isPending={details.isPending} />
          ) : (
            <p className="text-sm text-muted-foreground">{t('upload.match.nothingSelected')}</p>
          )}
          {selected && isTv && (
            <>
              <ChoiceCards
                label={t('upload.match.kindLabel')}
                className={cn('border-t pt-4', kindChoices.length === 2 && 'grid-cols-2')}
                showBadge={false}
                choices={kindChoices}
                value={kind}
                onSelect={(value) => {
                  setKind(value)
                  if (value !== 'complete_pack') setSeasons(seasons.slice(0, 1))
                }}
              />
              {orders.data && activeOrder && orders.data.orders.length > 1 && (
                <EpisodeOrderPicker data={orders.data} active={activeOrder} onChange={changeOrder} />
              )}
              <SeasonPicker
                rows={seasonRows}
                catalog={activeOrder != null || details.data != null}
                found={found}
                detected={detected}
                kind={kind}
                seasons={seasons}
                onSeasonsChange={setSeasons}
                episode={episode}
                onEpisodeChange={setEpisode}
              />
            </>
          )}
          {selected && !isTv && job.kind !== 'movie' && (
            <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
              <TriangleAlertIcon className="size-3.5" />
              {t('upload.match.movieButSeriesDetected')}
            </p>
          )}
          <Button disabled={invalid || confirm.isPending || split.isPending} onClick={submit}>
            {t('upload.match.confirm')}
          </Button>
          {canSplit && (
            <div className="grid gap-1.5">
              <p className="text-xs text-muted-foreground">
                {t('upload.match.splitHelp', { found: foundInSeason, expected })}
              </p>
              <Button variant="outline" disabled={confirm.isPending || split.isPending} onClick={splitIntoEpisodes}>
                {t('upload.match.split', { count: videos })}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
