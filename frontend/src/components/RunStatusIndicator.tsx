import {
  AlertCircleIcon,
  CheckCircle2Icon,
  ChevronDownIcon,
  ChevronUpIcon,
  CircleDashedIcon,
  CircleIcon,
  Loader2Icon,
  SquareIcon,
  XIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { useRuns } from '@/api/hooks/runs'
import { Progress } from '@/components/ui/progress'
import { FieryEye } from '@/components/FieryEye'
import { t } from '@/lib/i18n'
import {
  etaSeconds,
  formatCount,
  formatDuration,
  percent,
  runSteps,
  type RunResponse,
  type Step,
} from '@/lib/run-progress'
import { cn } from '@/lib/utils'
import { parseApiDate } from '@/lib/time'

const EXPANDED_STORAGE_KEY = 'runStatus.expanded'
// L'ultima scansione chiusa a mano (X): il riepilogo di una scansione finita
// resta finché l'utente non lo chiude, anche dopo una ricarica della pagina.
const DISMISSED_STORAGE_KEY = 'runStatus.dismissedRunId'

function readDismissed(): number {
  try {
    return Number(localStorage.getItem(DISMISSED_STORAGE_KEY)) || 0
  } catch {
    return 0
  }
}

function writeDismissed(runId: number) {
  try {
    localStorage.setItem(DISMISSED_STORAGE_KEY, String(runId))
  } catch {
    // senza storage il riepilogo si chiude comunque, solo per questa sessione
  }
}

function readExpanded(): boolean {
  try {
    return localStorage.getItem(EXPANDED_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

function writeExpanded(value: boolean) {
  try {
    localStorage.setItem(EXPANDED_STORAGE_KEY, String(value))
  } catch {
    // preferenza solo di comodità: senza storage resta com'è in questa sessione
  }
}

// Ridisegna ogni secondo mentre la run è attiva: tempo trascorso e stima
// cambiano anche tra un poll e l'altro (ogni 3s, useRuns).
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

function stepSummary(step: Step): string | null {
  const p = step.progress
  if (!p) return null
  const count = t(`runStatus.unit.${step.phase}`, { count: formatCount(p.done) })
  return p.skipped ? `${count} · ${t('runStatus.skipped', { count: formatCount(p.skipped) })}` : count
}

function StepIcon({ state }: { state: Step['state'] }) {
  if (state === 'done') return <CheckCircle2Icon className="size-3.5 text-emerald-600 dark:text-emerald-400" />
  if (state === 'running') return <Loader2Icon className="size-3.5 animate-spin text-primary" />
  if (state === 'skipped') return <CircleDashedIcon className="size-3.5 text-muted-foreground/60" />
  return <CircleIcon className="size-3.5 text-muted-foreground/40" />
}

function CurrentPhase({ run, step, now }: { run: RunResponse; step: Step; now: number }) {
  const p = step.progress
  const pct = percent(p?.done, p?.total)
  const eta = etaSeconds(p, now)
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-1 pl-5">
      {run.phase_detail && (
        // Es. "Verifying <nome del torrent>": fino a due righe, spezzando anche
        // i nomi senza spazi, il testo intero nel tooltip.
        <p className="line-clamp-2 text-xs text-muted-foreground [overflow-wrap:anywhere]" title={run.phase_detail}>
          {run.phase_detail}
        </p>
      )}
      {p && p.total != null && p.total > 0 ? (
        <>
          <Progress value={pct ?? 0} />
          <div className="flex items-center justify-between text-xs tabular-nums text-muted-foreground">
            <span>
              {formatCount(p.done)} / {formatCount(p.total)} · {pct}%
            </span>
            {eta != null && eta > 0 && <span>{t('runStatus.eta', { time: formatDuration(eta) })}</span>}
          </div>
          {p.skipped > 0 && (
            <p className="text-xs text-muted-foreground">
              {t('runStatus.skippedRecently', { count: formatCount(p.skipped) })}
            </p>
          )}
        </>
      ) : (
        <Progress value={null} />
      )}
    </div>
  )
}

function Stepper({ run, now }: { run: RunResponse; now: number }) {
  return (
    <ol className="grid grid-cols-[minmax(0,1fr)] gap-1.5">
      {runSteps(run).map((step) => (
        <li key={step.phase} className="grid grid-cols-[minmax(0,1fr)] gap-1">
          <div className="flex items-center gap-2 text-xs">
            <StepIcon state={step.state} />
            <span
              className={cn(
                'flex-1',
                step.state === 'running' && 'font-medium',
                (step.state === 'pending' || step.state === 'skipped') && 'text-muted-foreground',
              )}
            >
              {t(`runStatus.phase.${step.phase}`)}
            </span>
            {step.state === 'done' && (
              <span className="text-muted-foreground tabular-nums">{stepSummary(step)}</span>
            )}
          </div>
          {step.state === 'running' && <CurrentPhase run={run} step={step} now={now} />}
        </li>
      ))}
    </ol>
  )
}

function compactLine(run: RunResponse, active: boolean): string {
  if (!active) return t('runStatus.summary', { scanned: formatCount(run.items_scanned), errors: run.errors })
  const phase = run.current_phase ? t(`runStatus.phase.${run.current_phase}`) : t('runStatus.starting')
  const pct = percent(run.phase_done, run.phase_total)
  if (run.phase_total == null || run.phase_total <= 0 || pct == null) return phase
  return `${phase} · ${formatCount(run.phase_done ?? 0)}/${formatCount(run.phase_total)} · ${pct}%`
}

// Vive nel layout globale (AppLayout), non in una singola pagina: sopravvive
// al cambio view mentre una run è in corso. A scansione finita il riepilogo
// resta visibile finché l'utente non lo chiude (X), anche se la scansione è
// finita a pagina chiusa: l'id chiuso è ricordato nel browser.
// Avanzamento per fase da nazgarr/run_progress.py.
export function RunStatusIndicator() {
  const { data: runs } = useRuns()
  const latestRun = runs?.[0]
  const isActive = latestRun != null && latestRun.finished_at == null

  const [dismissedRunId, setDismissedRunId] = useState(readDismissed)
  const [expanded, setExpanded] = useState(readExpanded)
  const now = useNow(isActive)
  const dismiss = (runId: number) => {
    writeDismissed(runId)
    setDismissedRunId(runId)
  }

  const run = isActive || (latestRun != null && latestRun.id > dismissedRunId) ? latestRun : undefined
  if (!run) return null

  const stopped = !isActive && run.cancelled
  const hasErrors = !isActive && !stopped && run.errors > 0
  const elapsedEnd = run.finished_at ? parseApiDate(run.finished_at).getTime() : now
  const elapsed = (elapsedEnd - parseApiDate(run.started_at).getTime()) / 1000
  const toggle = () => {
    setExpanded((value) => {
      writeExpanded(!value)
      return !value
    })
  }

  return (
    <div className="w-80 max-w-[calc(100vw-2rem)] rounded-lg border bg-card text-sm shadow-lg">
      <div className="flex items-start gap-3 px-4 py-3">
        {isActive ? (
          <FieryEye className="-ml-1 h-5 w-8 shrink-0" />
        ) : stopped ? (
          <SquareIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        ) : hasErrors ? (
          <AlertCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
        ) : (
          <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
        )}
        <div className="min-w-0 flex-1">
          <p className="flex items-center justify-between gap-2 font-medium">
            <span className="truncate">
              {isActive
                ? run.cancel_requested
                  ? t('runStatus.stopping', { id: run.id })
                  : t('runStatus.inProgress', { id: run.id })
                : stopped
                  ? t('runStatus.stopped')
                  : hasErrors
                  ? t('runStatus.completedWithErrors')
                  : t('runStatus.completed')}
            </span>
            <span className="shrink-0 text-xs font-normal text-muted-foreground tabular-nums">
              {formatDuration(elapsed)}
            </span>
          </p>
          {!expanded && <p className="truncate text-xs text-muted-foreground">{compactLine(run, isActive)}</p>}
        </div>
        <button
          type="button"
          onClick={toggle}
          className="shrink-0 text-muted-foreground hover:text-foreground"
          aria-label={expanded ? t('runStatus.collapse') : t('runStatus.expand')}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDownIcon className="size-4" /> : <ChevronUpIcon className="size-4" />}
        </button>
        {!isActive && (
          <button
            type="button"
            onClick={() => dismiss(run.id)}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label={t('runStatus.dismiss')}
          >
            <XIcon className="size-4" />
          </button>
        )}
      </div>

      {expanded && (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-3 border-t px-4 py-3">
          <Stepper run={run} now={now} />
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t pt-2 text-xs text-muted-foreground tabular-nums">
            <span>{t('runStatus.candidates', { count: formatCount(run.matches_found) })}</span>
            <span>{t('runStatus.executed', { count: formatCount(run.auto_executed) })}</span>
            <span className={cn(run.errors > 0 && 'text-destructive')}>
              {t('runStatus.errors', { count: run.errors })}
            </span>
          </div>
          {(hasErrors || stopped) && run.last_error && (
            <p className={cn('text-xs', stopped ? 'text-muted-foreground' : 'text-destructive')}>{run.last_error}</p>
          )}
          {!isActive && run.pending_review > 0 && (
            <Link to="/reseeding" className="text-xs font-medium text-primary hover:underline">
              {t('runStatus.reviewPending', { count: run.pending_review })}
            </Link>
          )}
        </div>
      )}
    </div>
  )
}
