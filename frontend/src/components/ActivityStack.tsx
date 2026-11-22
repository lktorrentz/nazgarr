import { useQueryClient } from '@tanstack/react-query'
import { AlertCircleIcon, CheckCircle2Icon, InfoIcon, Loader2Icon, XIcon } from 'lucide-react'
import { useEffect, useRef } from 'react'

import type { Schemas } from '@/api/client'
import { isActiveCheck as isActive, useFullChecks } from '@/api/hooks/fullChecks'
import { useRecentSeedJobs } from '@/api/hooks/reviews'
import { Progress } from '@/components/ui/progress'
import {
  dismissActivity,
  hasActivity,
  pushActivity,
  updateActivity,
  useActivities,
  type Activity,
  type ActivityStatus,
} from '@/lib/activity'
import { formatBytes } from '@/lib/library-filters'
import {
  activityForCheck,
  noteQueued,
  trackVerification,
  untrackVerification,
  wasQueued,
} from '@/lib/verification'
import { t } from '@/lib/i18n'
import { cn } from '@/lib/utils'

const ICONS: Record<ActivityStatus, React.ReactNode> = {
  running: <Loader2Icon className="mt-0.5 size-4 shrink-0 animate-spin text-primary" />,
  success: <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />,
  error: <AlertCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />,
  info: <InfoIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />,
}

// Avvisa quando un recheck finisce in background (reconcile periodico dello
// scheduler, "Check now", una run): confronta lo stato delle ultime
// esecuzioni fra un poll e l'altro. Al primo caricamento non annuncia
// niente — solo i cambi avvenuti mentre l'app è aperta.
function useSeedJobWatcher() {
  const { data: jobs } = useRecentSeedJobs()
  const previous = useRef<Map<number, string> | null>(null)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!jobs) return
    const before = previous.current
    previous.current = new Map(jobs.map((job) => [job.id, job.final_status]))
    if (before == null) return
    let changed = false
    for (const job of jobs) {
      if (before.get(job.id) !== 'in_progress' || job.final_status === 'in_progress') continue
      changed = true
      if (job.final_status === 'seeding') {
        pushActivity({ status: 'success', title: t('activity.nowSeeding'), detail: job.candidate_name ?? undefined })
      } else {
        pushActivity({
          status: 'error',
          title: t('activity.recheckFailed'),
          detail: job.error_message ?? job.candidate_name ?? undefined,
        })
      }
    }
    // Episodi e file passati a seeding: le viste della libreria si aggiornano.
    if (changed) queryClient.invalidateQueries({ queryKey: ['library'] })
  }, [jobs, queryClient])
}

type FullCheck = Schemas['FullCheckResponse']

function runningPatch(check: FullCheck): Partial<Activity> {
  if (check.status === 'queued') return { title: t('activity.verifyQueued'), progress: 0 }
  if (check.stage === 'executing') return { title: t('activity.verifyPassedAdding'), progress: 100 }
  if (!check.bytes_total) return { title: t('activity.verifyDownloading'), progress: 0 }
  const pct = (100 * check.bytes_done) / check.bytes_total
  return {
    title: t('activity.verifying', { percent: pct.toFixed(0) }),
    detail: `${formatBytes(check.bytes_done)} / ${formatBytes(check.bytes_total)} · ${check.label}`,
    progress: pct,
  }
}

function finalPatch(check: FullCheck): Partial<Activity> {
  const done = { progress: null, detail: check.label }
  if (check.status === 'cancelled') return { ...done, status: 'info', title: t('activity.verifyCancelled') }
  if (check.status === 'failed') {
    return { ...done, status: 'error', title: t('activity.verifyCouldNotRun'), detail: check.error ?? check.label }
  }
  if (check.verdict !== 'passed') {
    return { ...done, status: 'error', title: t('activity.verifyFailed'), detail: check.verdict_reason ?? check.label }
  }
  if (check.execution === 'added') return { ...done, status: 'success', title: t('activity.verifyPassedAdded') }
  if (check.execution === 'no_client') return { ...done, status: 'info', title: t('activity.approvedNoClient') }
  if (check.execution === 'failed') {
    return { ...done, status: 'error', title: t('activity.executionFailed'), detail: check.execution_error ?? check.label }
  }
  return { ...done, status: 'info', title: t('activity.verifyPassedSkipped') }
}

// Segue i controlli completi partiti da un'approvazione (verify_before_execute):
// avanzamento nella notifica, poi l'esito e l'eventuale aggiunta al client.
// Anche quelli già in corso quando si apre la pagina (ricarica, altra scheda).
// Quelli in coda senza una notifica loro ("Approva tutti", una stagione
// intera: centinaia, uno alla volta) stanno in una notifica sola, "N in
// coda"; ognuno prende la sua quando parte (2026-11-22).
function useVerificationWatcher() {
  const queryClient = useQueryClient()
  const { data: checks } = useFullChecks()
  const queuedSummary = useRef<string | null>(null)

  useEffect(() => {
    if (!checks) return
    let finished = false
    let queued = 0
    for (const check of checks) {
      if (check.purpose !== 'verify') continue
      let activityId = activityForCheck(check.id)
      if (activityId == null) {
        if (check.status === 'queued') {
          queued += 1
          noteQueued(check.id)
          continue
        }
        if (!isActive(check) && !wasQueued(check.id)) continue // finito prima che la pagina lo seguisse: niente da annunciare
        activityId = pushActivity({ status: 'running', title: t('activity.verifyQueued'), detail: check.label })
        trackVerification(check.id, activityId)
      }
      if (isActive(check)) {
        updateActivity(activityId, runningPatch(check))
      } else {
        updateActivity(activityId, finalPatch(check))
        untrackVerification(check.id)
        finished = true
      }
    }
    const summary = queuedSummary.current
    if (queued === 0) {
      if (summary) dismissActivity(summary)
      queuedSummary.current = null
    } else {
      const patch = { status: 'running' as const, title: t('activity.verifyQueuedMany', { count: queued }), progress: null }
      if (summary && hasActivity(summary)) updateActivity(summary, patch)
      else queuedSummary.current = pushActivity(patch)
    }
    if (finished) {
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    }
  }, [checks, queryClient])
}

export function ActivityStack() {
  const activities = useActivities()
  useSeedJobWatcher()
  useVerificationWatcher()
  if (activities.length === 0) return null
  return (
    // minmax(0,1fr): senza, la colonna della griglia si allarga fino alla
    // parola più lunga (un nome di file senza spazi) e la card sborda.
    <div className="grid w-80 max-w-[calc(100vw-2rem)] grid-cols-[minmax(0,1fr)] gap-2 max-sm:w-full">
      {activities.map((activity) => (
        <div
          key={activity.id}
          role={activity.status === 'error' ? 'alert' : 'status'}
          className="flex min-w-0 items-start gap-3 rounded-lg border bg-card px-4 py-3 text-sm shadow-lg"
        >
          {ICONS[activity.status]}
          <div className="min-w-0 flex-1">
            <p className="font-medium [overflow-wrap:anywhere]">{activity.title}</p>
            {activity.detail && (
              // Fino a due righe, spezzando anche i nomi senza spazi; il testo
              // intero resta nel tooltip.
              <p
                className={cn(
                  'line-clamp-2 text-xs [overflow-wrap:anywhere]',
                  activity.status === 'error' ? 'text-destructive' : 'text-muted-foreground',
                )}
                title={activity.detail}
              >
                {activity.detail}
              </p>
            )}
            {activity.progress != null && <Progress value={activity.progress} className="mt-2" />}
          </div>
          {activity.status !== 'running' && (
            <button
              type="button"
              onClick={() => dismissActivity(activity.id)}
              className="-my-1.5 inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:text-foreground pointer-coarse:size-9"
              aria-label={t('runStatus.dismiss')}
            >
              <XIcon className="size-4" />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
