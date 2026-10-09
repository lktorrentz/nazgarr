import { ArrowDownIcon, ArrowUpIcon, CircleXIcon, PlusIcon, RotateCcwIcon, Trash2Icon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { posterUrl } from '@/api/hooks/metadata'
import { useCancelUpload, useDeleteUpload, useReorderQueue, useResumeUpload, useUploads, type UploadJobSummary } from '@/api/hooks/uploads'
import { AuthedPoster } from '@/components/AuthedPoster'
import { ActionBadge } from '@/components/upload/TrackerCheckCard'
import { UploadDetailSheet } from '@/components/upload/UploadDetailSheet'
import { UploadStatusBadge } from '@/components/upload/UploadStatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useRingLoader } from '@/components/RingLoader'
import { t } from '@/lib/i18n'
import { formatWhen, isFuture } from '@/lib/schedule'
import { sourceLabel } from '@/lib/upload'
import { parseApiDate } from '@/lib/time'
import { cn } from '@/lib/utils'

const FINAL_STATES = ['done', 'partial', 'failed', 'cancelled']
// In corso: prima quello che gira, poi la coda in ordine, poi quelli che
// aspettano l'utente, poi quelli che il worker sta identificando/analizzando.
const ACTIVE_ORDER = ['running', 'queued', 'awaiting_decision', 'awaiting_match', 'analyzing', 'identifying']

function title(job: UploadJobSummary) {
  return job.title ? `${job.title}${job.year ? ` (${job.year})` : ''}` : t('upload.untitled')
}

function Poster({ job }: { job: UploadJobSummary }) {
  if (!job.tmdb_id || !job.content_type) return <div className="aspect-[2/3] w-11 shrink-0 rounded bg-muted" />
  return (
    <AuthedPoster
      contentType={job.content_type}
      tmdbId={job.tmdb_id}
      hasPoster
      url={posterUrl({ content_type: job.content_type as 'movie' | 'tv', tmdb_id: job.tmdb_id, poster_path: job.poster_path })}
      className="aspect-[2/3] w-11 shrink-0 overflow-hidden rounded"
    />
  )
}

// Un tracker per riga nella colonna di destra (in fila sotto il titolo da
// telefono e tablet): con più tracker l'esito di ognuno resta leggibile.
function TargetOutcomes({ job, className }: { job: UploadJobSummary; className?: string }) {
  return (
    <div className={cn('flex flex-wrap gap-1.5 lg:grid lg:content-center lg:justify-items-start lg:gap-1', className)}>
      {job.targets.map((target) => (
        <span key={target.id} className="inline-flex items-center gap-1 text-xs">
          <span className="text-muted-foreground">{target.tracker_label}</span>
          {target.action && FINAL_STATES.includes(job.status) ? (
            target.status === 'failed' ? (
              <Badge variant="outline" className="border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300">
                {t('upload.status.failed')}
              </Badge>
            ) : (
              <ActionBadge action={target.action} />
            )
          ) : (
            <UploadStatusBadge status={target.status} className="h-5 text-[10px]" />
          )}
        </span>
      ))}
    </div>
  )
}

// Titolo, badge e sorgente: la parte che cresce, a sinistra.
function JobHeading({ job, status }: { job: UploadJobSummary; status?: React.ReactNode }) {
  return (
    // Sotto lg tutta la prima riga accanto al poster (w-11 + gap-x-4): esiti e
    // azioni vanno sulla seconda, invece di schiacciare il titolo.
    <div className="grid min-w-0 flex-1 gap-1 max-lg:basis-[calc(100%-3.75rem)]">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="truncate font-medium">{title(job)}</span>
        {job.kind && <Badge variant="outline">{t(`upload.kind.${job.kind}`)}</Badge>}
        {job.origin === 'watch' && <Badge variant="outline">{t('upload.watch.badge')}</Badge>}
        {status}
      </div>
      <p className="truncate font-mono text-xs text-muted-foreground" title={sourceLabel(job)}>
        {sourceLabel(job)}
      </p>
    </div>
  )
}

// Ogni upload una riga a sé, senza un contenitore intorno (decisione
// dell'utente, 2026-10-02): poster, titolo e sorgente, esito per tracker,
// data o avanzamento, azioni. Su una riga sola solo da lg: fra sm e lg le
// colonne fisse lasciavano al titolo pochi pixel.
const ROW = 'flex cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border bg-card p-3 transition-colors hover:bg-muted/50 lg:flex-nowrap'

function ActiveList({ jobs }: { jobs: UploadJobSummary[] }) {
  const navigate = useNavigate()
  const reorder = useReorderQueue()
  const queued = jobs.filter((job) => job.status === 'queued')

  function move(job: UploadJobSummary, delta: number) {
    const ids = queued.map((j) => j.id)
    const from = ids.indexOf(job.id)
    const to = from + delta
    if (to < 0 || to >= ids.length) return
    ;[ids[from], ids[to]] = [ids[to], ids[from]]
    reorder.mutate(ids, { onError: (error) => toast.error(error.message) })
  }

  if (jobs.length === 0) return <p className="py-10 text-center text-sm text-muted-foreground">{t('upload.history.noActive')}</p>
  return (
    <ul className="grid gap-2">
      {jobs.map((job) => {
        const pct = job.progress_total ? Math.round((100 * (job.progress_done ?? 0)) / job.progress_total) : null
        const index = queued.indexOf(job)
        return (
          <li key={job.id} className={ROW} onClick={() => navigate(`/upload/${job.id}`)}>
            <Poster job={job} />
            <JobHeading
              job={job}
              status={
                <>
                  <UploadStatusBadge status={job.status} />
                  {job.status === 'queued' && (
                    <span className="text-xs text-muted-foreground">
                      {isFuture(job.scheduled_at) ? t('upload.schedule.badge', { when: formatWhen(job.scheduled_at!) }) : `#${index + 1}`}
                    </span>
                  )}
                </>
              }
            />
            <TargetOutcomes job={job} className="min-w-0 flex-1 basis-[60%] lg:w-56 lg:flex-none lg:shrink-0 lg:basis-auto" />
            <div className="w-full max-lg:empty:hidden lg:w-40 lg:shrink-0">
              {job.status === 'running' && pct !== null && (
                <div className="grid gap-1">
                  <Progress value={pct} />
                  <span className="text-right text-xs text-muted-foreground tabular-nums">{pct}%</span>
                </div>
              )}
            </div>
            <div className="ml-auto flex shrink-0 items-center" onClick={(e) => e.stopPropagation()}>
              {job.status === 'queued' && queued.length > 1 && (
                // Al tocco affiancate: impilate, due bersagli grandi non ci stanno nella riga.
                <div className="flex flex-col pointer-coarse:flex-row">
                  <Button variant="ghost" size="icon-xs" disabled={index === 0} title={t('upload.history.moveUp')} onClick={() => move(job, -1)}>
                    <ArrowUpIcon className="size-4" />
                  </Button>
                  <Button variant="ghost" size="icon-xs" disabled={index === queued.length - 1} title={t('upload.history.moveDown')} onClick={() => move(job, 1)}>
                    <ArrowDownIcon className="size-4" />
                  </Button>
                </div>
              )}
              <QuickDelete job={job} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// Il job che il worker sta eseguendo (upload sui tracker in corso) si
// annulla e basta: finisce nello storico, da dove si elimina. Gli altri in
// corso (identificazione, analisi, in coda: passi senza effetti fuori da
// Nazgarr) si annullano ed eliminano insieme; quelli fermi a una decisione
// si eliminano e basta.
const CANCEL_FIRST = ['identifying', 'analyzing', 'queued']

// Cestino di una riga: il primo click chiede conferma sul posto (torna
// com'era dopo qualche secondo), il secondo agisce. Solo il record:
// tracker, client e disco restano come sono.
function QuickDelete({ job }: { job: UploadJobSummary }) {
  const remove = useDeleteUpload()
  const cancel = useCancelUpload()
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const timer = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(timer)
  }, [armed])
  const running = job.status === 'running'
  const label = running
    ? t('upload.history.cancelRunning')
    : FINAL_STATES.includes(job.status)
      ? t('upload.history.delete')
      : t('upload.history.removeActive')

  async function act() {
    if (running) {
      await cancel.mutateAsync(job.id)
      toast.success(t('upload.history.cancelled', { title: title(job) }))
      return
    }
    if (CANCEL_FIRST.includes(job.status)) await cancel.mutateAsync(job.id)
    await remove.mutateAsync(job.id)
    toast.success(t('upload.history.deleted', { title: title(job) }))
  }

  return (
    <Button
      variant={armed ? 'destructive' : 'ghost'}
      size={armed ? 'sm' : 'icon-sm'}
      className="shrink-0"
      disabled={remove.isPending || cancel.isPending}
      title={label}
      aria-label={label}
      onClick={(event) => {
        event.stopPropagation()
        if (!armed) return setArmed(true)
        act().catch((error: Error) => toast.error(error.message))
      }}
    >
      {armed ? (running ? t('upload.cancelJob') : t('upload.history.deleteConfirm')) : running ? <CircleXIcon /> : <Trash2Icon />}
    </Button>
  )
}

// Riprende un upload annullato dallo storico, senza aprirlo.
function QuickResume({ job }: { job: UploadJobSummary }) {
  const resume = useResumeUpload()
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className="shrink-0"
      disabled={resume.isPending}
      title={t('upload.resumeJob')}
      aria-label={t('upload.resumeJob')}
      onClick={() =>
        resume
          .mutateAsync(job.id)
          .then(() => toast.success(t('upload.history.resumed', { title: title(job) })))
          .catch((error: Error) => toast.error(error.message))
      }
    >
      <RotateCcwIcon />
    </Button>
  )
}

function HistoryList({ jobs, onOpen }: { jobs: UploadJobSummary[]; onOpen: (id: number) => void }) {
  if (jobs.length === 0) return <p className="py-10 text-center text-sm text-muted-foreground">{t('upload.noUploadsYet')}</p>
  return (
    <ul className="grid gap-2">
      {jobs.map((job) => (
        <li key={job.id} className={cn(ROW, job.status === 'cancelled' && 'opacity-60')} onClick={() => onOpen(job.id)}>
          <Poster job={job} />
          <JobHeading job={job} status={<UploadStatusBadge status={job.status} />} />
          <TargetOutcomes job={job} className="min-w-0 flex-1 basis-[60%] lg:w-56 lg:flex-none lg:shrink-0 lg:basis-auto" />
          <span className="text-xs text-muted-foreground tabular-nums lg:w-40 lg:shrink-0 lg:text-right">
            {job.finished_at && parseApiDate(job.finished_at).toLocaleString()}
          </span>
          <div className="ml-auto flex shrink-0" onClick={(e) => e.stopPropagation()}>
            {job.status === 'cancelled' && <QuickResume job={job} />}
            <QuickDelete job={job} />
          </div>
        </li>
      ))}
    </ul>
  )
}

export function UploadQueuePage() {
  const query = useUploads()
  const { data } = query
  const loader = useRingLoader(query)
  const navigate = useNavigate()
  const [openId, setOpenId] = useState<number | null>(null)
  const [params] = useSearchParams()

  const jobs = data ?? []
  const active = jobs
    .filter((job) => !FINAL_STATES.includes(job.status))
    .sort(
      (a, b) =>
        ACTIVE_ORDER.indexOf(a.status) - ACTIVE_ORDER.indexOf(b.status) ||
        (a.queue_position ?? 0) - (b.queue_position ?? 0) ||
        a.id - b.id,
    )
  const history = jobs.filter((job) => FINAL_STATES.includes(job.status))

  return (
    <div className="grid gap-4">
      {loader ? (
        loader
      ) : (
        <Tabs defaultValue={params.get('tab') ?? (active.length > 0 || history.length === 0 ? 'active' : 'history')}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <TabsList data-tour="views.uploads">
              <TabsTrigger value="active">{t('upload.history.activeTab', { count: active.length })}</TabsTrigger>
              <TabsTrigger value="history">{t('upload.history.historyTab', { count: history.length })}</TabsTrigger>
            </TabsList>
            <Button data-tour="upload.new" onClick={() => navigate('/upload/new')}>
              <PlusIcon className="size-4" />
              {t('upload.newUpload')}
            </Button>
          </div>
          <TabsContent value="active" className="pt-2">
            <ActiveList jobs={active} />
          </TabsContent>
          <TabsContent value="history" className="pt-2">
            <HistoryList jobs={history} onOpen={setOpenId} />
          </TabsContent>
        </Tabs>
      )}
      <UploadDetailSheet uploadId={openId} onClose={() => setOpenId(null)} />
    </div>
  )
}
