import { ChevronRightIcon, Loader2Icon, RotateCcwIcon, TrashIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import {
  useApproveReview,
  useCandidateAudit,
  useDecideReviews,
  useDeleteSeedJob,
  useRecentSeedJobs,
  useRejectReview,
  useRetryFailed,
  useReviews,
  useSetReviewClientLabels,
} from '@/api/hooks/reviews'
import { useTorrentClientCategories } from '@/api/hooks/torrentClients'
import { ClientCategorySelect } from '@/components/ClientCategorySelect'
import { ConfirmButton } from '@/components/ConfirmButton'
import { ErrorsPopover } from '@/components/ErrorsPopover'
import { FullCheckButton } from '@/components/FullCheckButton'
import { StateBadge } from '@/components/StateBadge'
import { VerifyStatus } from '@/components/VerifyStatus'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroupItem, ToggleGroupSingle } from '@/components/ui/toggle-group'
import { t } from '@/lib/i18n'
import { formatBytes } from '@/lib/library-filters'
import { episodeRanges, groupBySeason, seasonLabel } from '@/lib/reviewGroups'
import { parseApiDate, relativeFromNow } from '@/lib/time'
import { cn } from '@/lib/utils'

type Review = NonNullable<ReturnType<typeof useReviews>['data']>[number]
type Layout = NonNullable<Review['layout']>

// Dove il file è già in seed, tolto il tracker della review: lì è il secondo
// formato (pack e singoli), non un cross-seed.
function crossSeedTrackers(review: Review): string[] {
  return (review.seeding_on ?? []).filter((tracker) => tracker !== review.tracker)
}

function CrossSeedBadge({ trackers }: { trackers: string[] }) {
  return (
    <Badge variant="outline" title={t('reseeding.crossSeedHelp', { trackers })}>
      {t('reseeding.crossSeed', { trackers })}
    </Badge>
  )
}

// Già in seed su questo tracker nell'altro formato (nel pack, o come singoli).
function SecondFormatBadge({ review }: { review: Review }) {
  const key = review.format === 'pack' ? 'reseeding.secondFormatPack' : 'reseeding.secondFormatSingle'
  return (
    <Badge variant="outline" title={t('reseeding.secondFormatHelp')}>
      {t(key, { tracker: review.tracker ?? '' })}
    </Badge>
  )
}

// Riepilogo di un torrent multi-file (film con extra, season pack), dai
// dati di candidate_file: cosa verrà ricreato da file locali e cosa
// scaricherà il client dopo il recheck.
function layoutSummary(layout: Layout): string {
  const parts = [
    t(layout.video_count > 1 ? 'reseeding.seasonPack' : 'reseeding.withExtras'),
    t('reseeding.videosVerified', {
      verified: layout.videos_piece_verified,
      matched: layout.videos_matched,
      total: layout.video_count,
    }),
  ]
  if (layout.extras_missing > 0) {
    parts.push(
      t('reseeding.extrasToDownload', {
        count: layout.extras_missing,
        size: formatBytes(layout.extras_missing_bytes),
      }),
    )
  }
  return parts.join(' · ')
}

function LayoutFiles({ layout }: { layout: Layout }) {
  return (
    <div className="border-t bg-muted/30 p-3">
      <p className="mb-1.5 text-xs font-medium text-muted-foreground">{t('reseeding.torrentFiles')}</p>
      <Table>
        <TableHeader>
          <TableRow>
            {/* Sotto sm una sola colonna con i due percorsi, a capo: due
                colonne troncate non mostravano niente di utile. */}
            <TableHead>{t('reseeding.fileInTorrent')}</TableHead>
            <TableHead className="hidden sm:table-cell">{t('reseeding.localFile')}</TableHead>
            <TableHead className="w-24 text-right">{t('library.columnSize')}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {layout.files.map((f) => {
            const local = f.local_path ? (
              <span className={cn(f.piece_verified === false && 'text-destructive')}>
                {f.local_path}
                {f.piece_verified && <Badge variant="secondary" className="ml-1.5">{t('reseeding.verified')}</Badge>}
              </span>
            ) : (
              <span className="text-muted-foreground">
                {f.is_video ? t('reseeding.missingVideo') : t('reseeding.clientDownloads')}
              </span>
            )
            return (
              <TableRow key={f.torrent_path}>
                <TableCell className="max-w-0 font-mono text-xs max-sm:break-all max-sm:whitespace-normal sm:truncate" title={f.torrent_path}>
                  {f.torrent_path}
                  <span className="mt-0.5 block font-sans sm:hidden">{local}</span>
                </TableCell>
                <TableCell className="hidden max-w-0 truncate text-xs sm:table-cell" title={f.local_path ?? undefined}>
                  {local}
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums">
                  {f.size_bytes != null ? formatBytes(f.size_bytes) : '—'}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function CandidateAudit({ mediaItemId }: { mediaItemId: number }) {
  const { data, isPending } = useCandidateAudit(mediaItemId)

  if (isPending) return <p className="p-3 text-xs text-muted-foreground">{t('reseeding.loadingCandidates')}</p>

  return (
    <div className="grid gap-1.5 border-t bg-muted/30 p-3">
      <p className="text-xs font-medium text-muted-foreground">
        {t('reseeding.allCandidatesEvaluated')}
      </p>
      {data?.map((c) => (
        <div key={c.id} className="flex items-center justify-between gap-2 text-xs">
          <span className="min-w-0 truncate font-mono pointer-coarse:break-all pointer-coarse:whitespace-normal">{c.name}</span>
          <div className="flex shrink-0 items-center gap-2">
            {c.ambiguity_reason && <span className="text-muted-foreground">{c.ambiguity_reason}</span>}
            <span>{(c.confidence * 100).toFixed(0)}%</span>
          </div>
        </div>
      ))}
    </div>
  )
}

// Categoria e tag con cui il reseed entra nel client: i default del client,
// cambiabili a mano come per un upload. Si salvano subito e valgono quando
// il reseed parte.
function ReviewClientLabels({ review }: { review: Review }) {
  const { data } = useTorrentClientCategories(review.torrent_client_id ?? null)
  const save = useSetReviewClientLabels(review.id)
  const custom = review.client_category !== null || review.client_tags !== null
  const category = review.client_category !== null ? review.client_category || null : review.default_client_category ?? null
  const savedTags = review.client_tags ?? review.default_client_tags ?? ''
  const [tags, setTags] = useState<string | null>(null)
  if (review.torrent_client_id == null) return null
  const categories = data?.status === 'ok' ? data.categories : []
  const send = (next: { category?: string | null; tags?: string }) =>
    save.mutate(
      { client_category: next.category !== undefined ? next.category ?? '' : category ?? '', client_tags: next.tags ?? savedTags },
      { onSuccess: () => setTags(null), onError: (error) => toast.error(error.message) },
    )
  return (
    <div className="grid gap-2 border-t px-3 py-2 sm:pl-9">
      <p className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{t('reseeding.clientLabels')}</p>
      <div className="flex flex-wrap items-end gap-3">
        {(categories.length > 0 || category) && (
          <label className="grid gap-1 text-xs">
            <span className="text-muted-foreground">{t('reseeding.clientCategory')}</span>
            <ClientCategorySelect categories={categories} value={category} onChange={(value) => send({ category: value })} />
          </label>
        )}
        <label className="grid min-w-40 flex-1 gap-1 text-xs sm:max-w-xs">
          <span className="text-muted-foreground">{t('reseeding.clientTags')}</span>
          <Input
            className="h-8 text-xs"
            value={tags ?? savedTags}
            placeholder={t('torrentClients.noTags')}
            onChange={(e) => setTags(e.target.value)}
            onBlur={() => tags !== null && tags !== savedTags && send({ tags })}
          />
        </label>
        {custom && (
          <Button size="xs" variant="ghost" disabled={save.isPending}
                  onClick={() => save.mutate({ client_category: null, client_tags: null }, { onSuccess: () => setTags(null) })}>
            {t('reseeding.clientLabelsDefault')}
          </Button>
        )}
      </div>
    </div>
  )
}

function ReviewRow({ review }: { review: Review }) {
  const [open, setOpen] = useState(false)
  const approve = useApproveReview()
  const reject = useRejectReview()
  const verifying = review.verify_status === 'verifying'

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-b last:border-b-0">
      {/* Sotto sm Approva/Rifiuta vanno a capo, sotto il nome: accanto lo
          riducevano a poche lettere. */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 sm:flex-nowrap">
        <CollapsibleTrigger
          aria-label={review.candidate_name}
          className="-m-1 shrink-0 self-start rounded p-1 pointer-coarse:-m-2 pointer-coarse:p-2 sm:self-center"
        >
          <ChevronRightIcon className={cn('size-4 text-muted-foreground transition-transform', open && 'rotate-90')} />
        </CollapsibleTrigger>
        <div className="min-w-0 flex-1 basis-[calc(100%-2rem)] sm:basis-auto">
          <p className="truncate text-sm font-medium max-sm:break-all max-sm:whitespace-normal pointer-coarse:break-all pointer-coarse:whitespace-normal">
            {review.candidate_name}
          </p>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="secondary">{review.direction}</Badge>
            <span>{(review.confidence * 100).toFixed(0)}%</span>
            {review.ambiguity_reason && <span>{review.ambiguity_reason}</span>}
            {review.status === 'auto_approved' && <Badge>{t('reseeding.autoApproved')}</Badge>}
            {/* Il file seeda già altrove: è un cross-seed, non un file da salvare. */}
            {crossSeedTrackers(review).length > 0 && <CrossSeedBadge trackers={crossSeedTrackers(review)} />}
            {(review.seeding_here?.length ?? 0) > 0 && <SecondFormatBadge review={review} />}
          </div>
          {review.layout && <p className="text-xs text-muted-foreground">{layoutSummary(review.layout)}</p>}
          <VerifyStatus review={review} />
        </div>
        <div className="flex shrink-0 gap-2 max-sm:ml-6">
        <Button
          size="sm"
          variant="outline"
          disabled={verifying || approve.isPending}
          title={verifying ? t('reseeding.verifyingHint') : undefined}
          onClick={() => approve.mutate(review.id)}
        >
          {verifying && <Loader2Icon className="size-4 animate-spin" />}
          {verifying ? t('reseeding.verifying') : t('reseeding.approve')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() =>
            reject.mutate(review.id)
          }
        >
          {t('reseeding.reject')}
        </Button>
        </div>
      </div>
      <CollapsibleContent>
        <ReviewClientLabels review={review} />
        {review.layout && <LayoutFiles layout={review.layout} />}
        <CandidateAudit mediaItemId={review.media_item_id} />
      </CollapsibleContent>
    </Collapsible>
  )
}

// Gli episodi singoli di una stagione sullo stesso tracker, in una riga: il
// dettaglio di ognuno si apre sotto, perché non sempre ci sono tutti.
function SeasonRow({ reviews }: { reviews: Review[] }) {
  const [open, setOpen] = useState(false)
  const decide = useDecideReviews()
  const first = reviews[0]
  const percents = reviews.map((r) => Math.round(r.confidence * 100))
  const low = Math.min(...percents)
  const high = Math.max(...percents)
  const recommended = reviews.filter((r) => r.status === 'auto_approved').length
  const toApprove = reviews.filter((r) => r.verify_status !== 'verifying').map((r) => r.id)
  const crossSeed = [...new Set(reviews.flatMap(crossSeedTrackers))]
  const secondFormat = reviews.find((r) => (r.seeding_here?.length ?? 0) > 0)
  const name = `${first.title ?? first.candidate_name} · ${seasonLabel(first.season_number ?? 0)}`
  const episodes = episodeRanges(reviews.map((r) => r.episode_number ?? 0))

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-b last:border-b-0">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 sm:flex-nowrap">
        <CollapsibleTrigger
          aria-label={name}
          className="-m-1 shrink-0 self-start rounded p-1 pointer-coarse:-m-2 pointer-coarse:p-2 sm:self-center"
        >
          <ChevronRightIcon className={cn('size-4 text-muted-foreground transition-transform', open && 'rotate-90')} />
        </CollapsibleTrigger>
        <div className="min-w-0 flex-1 basis-[calc(100%-2rem)] sm:basis-auto">
          <p className="truncate text-sm font-medium max-sm:whitespace-normal pointer-coarse:whitespace-normal">{name}</p>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="secondary">{first.direction}</Badge>
            <span>{low === high ? `${low}%` : `${low}–${high}%`}</span>
            {recommended > 0 && (
              <Badge>
                {recommended === reviews.length
                  ? t('reseeding.autoApproved')
                  : t('reseeding.recommendedCount', { count: recommended })}
              </Badge>
            )}
            {crossSeed.length > 0 && <CrossSeedBadge trackers={crossSeed} />}
            {secondFormat && <SecondFormatBadge review={secondFormat} />}
          </div>
          <p className="text-xs text-muted-foreground">
            {t('reseeding.seasonSingles', { count: reviews.length, tracker: first.tracker ?? '' })}
            {' · '}
            {t('reseeding.seasonEpisodes', { episodes })}
          </p>
        </div>
        <div className="flex shrink-0 gap-2 max-sm:ml-6">
          <Button
            size="sm"
            variant="outline"
            disabled={decide.isPending || toApprove.length === 0}
            onClick={() => decide.mutate({ ids: toApprove, action: 'approve' })}
          >
            {decide.isPending && <Loader2Icon className="size-4 animate-spin" />}
            {t('reseeding.approveAll')}
          </Button>
          <ConfirmButton
            trigger={
              <Button size="sm" variant="ghost" disabled={decide.isPending}>
                {t('reseeding.rejectAll')}
              </Button>
            }
            title={t('reseeding.rejectAllTitle', { count: reviews.length })}
            description={t('reseeding.rejectAllDescription')}
            confirmLabel={t('reseeding.rejectAll')}
            pending={decide.isPending}
            onConfirm={() => decide.mutate({ ids: reviews.map((r) => r.id), action: 'reject' })}
          />
        </div>
      </div>
      <CollapsibleContent>
        <div className="border-t bg-muted/20 sm:pl-6">
          {reviews.map((r) => (
            <ReviewRow key={r.id} review={r} />
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

function ReviewCard() {
  const { data: reviews, isPending } = useReviews()

  return (
    <Card data-tour="views.review">
      <CardHeader>
        <CardTitle>{t('misc.review')}</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {isPending && <p className="p-3 text-sm text-muted-foreground">{t('common.loading')}</p>}
        {groupBySeason(reviews ?? []).map((group) =>
          group.length > 1 ? (
            <SeasonRow key={`season-${group[0].id}`} reviews={group} />
          ) : (
            <ReviewRow key={group[0].id} review={group[0]} />
          ),
        )}
        {reviews?.length === 0 && <p className="p-3 text-sm text-muted-foreground">{t('reseeding.noPendingReviews')}</p>}
      </CardContent>
    </Card>
  )
}

type SeedJob = NonNullable<ReturnType<typeof useRecentSeedJobs>['data']>[number]
type ExecutionFilter = 'all' | 'seeding' | 'in_progress' | 'failed'

const FILTERS: ExecutionFilter[] = ['all', 'seeding', 'in_progress', 'failed']

function ExecutionRow({ job }: { job: SeedJob }) {
  const retry = useRetryFailed()
  const remove = useDeleteSeedJob()
  const when = job.torrent_added_at ?? job.hardlink_created_at
  return (
    <TableRow>
      <TableCell className="max-w-0 w-full">
        <p className="truncate font-mono text-xs pointer-coarse:break-all pointer-coarse:whitespace-normal" title={job.candidate_name ?? undefined}>
          {job.candidate_name ?? t('reseeding.candidateHash', { id: job.candidate_id })}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {[job.tracker, job.torrent_client, job.direction && t(`reseeding.direction.${job.direction}`)]
            .filter(Boolean)
            .join(' · ')}
        </p>
        {/* Sotto sm la colonna Aggiunto non c'è: il quando va qui. */}
        {when && <p className="text-xs text-muted-foreground sm:hidden">{relativeFromNow(when)}</p>}
      </TableCell>
      <TableCell className="hidden whitespace-nowrap text-xs text-muted-foreground sm:table-cell" title={when ? parseApiDate(when).toLocaleString() : undefined}>
        {when ? relativeFromNow(when) : '—'}
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1.5">
          <StateBadge state={job.display_status ?? job.final_status} compact />
          {job.recheck_skipped && (
            <span className="text-[length:var(--text-xxs)] text-muted-foreground" title={t('reseeding.recheckSkippedHint')}>
              {t('reseeding.recheckSkipped')}
            </span>
          )}
          {job.error_message && (
            <ErrorsPopover count={1} messages={[job.error_message]} title={t('reseeding.executionError')} />
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-1.5">
          {(job.display_status ?? job.final_status) !== 'seeding' && (
            <FullCheckButton
              target={{ candidateId: job.candidate_id, seedJobId: job.id }}
              label={job.candidate_name ?? t('reseeding.candidateHash', { id: job.candidate_id })}
              iconOnlyOnPhone
            />
          )}
          {job.final_status === 'failed' && (
            <Button
              size="xs"
              variant="outline"
              title={t('reseeding.retry')}
              disabled={retry.isPending}
              onClick={() =>
                retry.mutate(job.id, {
                  onSuccess: () => toast.success(t('reseeding.retryCompleted')),
                  onError: (error) => toast.error(t('reseeding.retryFailed', { message: error.message })),
                })
              }
            >
              <RotateCcwIcon className="size-3" />
              {/* Sotto sm solo l'icona: con le etichette il nome del torrent spariva. */}
              <span className="max-sm:sr-only">{t('reseeding.retry')}</span>
            </Button>
          )}
          {/* Fallita o rimasta in corso: eliminarla libera il torrent per il
              prossimo scan (un candidato con un'esecuzione non torna in coda). */}
          {(job.final_status === 'failed' || job.final_status === 'in_progress') && (
            <ConfirmButton
              trigger={
                <Button size="xs" variant="ghost" title={t('reseeding.deleteJob')} aria-label={t('reseeding.deleteJob')}>
                  <TrashIcon className="size-3" />
                </Button>
              }
              title={t('reseeding.deleteJobTitle')}
              description={t('reseeding.deleteJobDescription')}
              pending={remove.isPending}
              onConfirm={() =>
                remove.mutate(job.id, {
                  onSuccess: () => toast.success(t('reseeding.jobDeleted')),
                  onError: (error) => toast.error(t('reseeding.deleteJobFailed', { message: error.message })),
                })
              }
            />
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}

// Tutte le esecuzioni (hardlink + torrent aggiunto al client), non solo
// quelle fallite: si vede anche cosa è tornato in seed e cosa aspetta il recheck.
function ExecutionsCard() {
  const { data: jobs, isPending } = useRecentSeedJobs()
  const [filter, setFilter] = useState<ExecutionFilter>('all')
  // "removed" (era in seed, torrent tolto dal client) conta solo in All.
  const statusOf = (j: SeedJob) => j.display_status ?? j.final_status
  const count = (f: ExecutionFilter) => (f === 'all' ? jobs?.length ?? 0 : jobs?.filter((j) => statusOf(j) === f).length ?? 0)
  const shown = jobs?.filter((j) => filter === 'all' || statusOf(j) === filter)

  return (
    <Card>
      {/* Sotto sm il filtro va sotto il titolo, a tutta larghezza, e scorre:
          a destra del titolo veniva tagliato. */}
      <CardHeader className="max-sm:grid-cols-1!">
        <CardTitle>{t('reseeding.executions')}</CardTitle>
        <CardDescription>{t('reseeding.executionsHint')}</CardDescription>
        <CardAction className="scroll-strip max-w-full max-sm:col-start-1 max-sm:row-span-1 max-sm:row-start-3 max-sm:justify-self-stretch">
          <ToggleGroupSingle value={filter} onValueChange={(v) => setFilter(v as ExecutionFilter)} variant="outline" size="sm">
            {FILTERS.map((f) => (
              <ToggleGroupItem key={f} value={f}>
                {t(`reseeding.filter.${f}`)}
                <span className="ml-1 font-mono text-xs text-muted-foreground tabular-nums">{count(f)}</span>
              </ToggleGroupItem>
            ))}
          </ToggleGroupSingle>
        </CardAction>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('reseeding.torrent')}</TableHead>
              <TableHead className="hidden sm:table-cell">{t('reseeding.added')}</TableHead>
              <TableHead>{t('reseeding.status')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {isPending && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-sm text-muted-foreground">
                  {t('common.loading')}
                </TableCell>
              </TableRow>
            )}
            {shown?.map((job) => <ExecutionRow key={job.id} job={job} />)}
            {shown?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-sm text-muted-foreground">
                  {t('reseeding.noExecutions')}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

export function ReseedingPage() {
  return (
    <div className="grid gap-6">
      <ReviewCard />
      <ExecutionsCard />
    </div>
  )
}
