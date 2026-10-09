import { CalendarClockIcon, LoaderCircleIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { useScheduleUpload, type UploadJob } from '@/api/hooks/uploads'
import { ExecutionSteps } from '@/components/upload/ExecutionSteps'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { t } from '@/lib/i18n'
import { formatWhen, fromLocalInput, isFuture, localInputValue } from '@/lib/schedule'
import { parseApiDate } from '@/lib/time'
import { executionSteps } from '@/lib/upload'

// Un upload programmato, in coda fino alla sua ora: si può anticipare
// ("Avvia ora") o spostare.
function ScheduledStep({ job }: { job: UploadJob }) {
  const schedule = useScheduleUpload(job.id)
  const [draft, setDraft] = useState<string | null>(null)
  const send = (value: string | null) =>
    schedule.mutate(value, {
      onSuccess: () => setDraft(null),
      onError: (error) => toast.error(t('upload.schedule.failed', { message: error.message })),
    })
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <CalendarClockIcon className="size-4 text-primary" />
          {t('upload.schedule.title', { when: formatWhen(job.scheduled_at!) })}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <p className="text-sm text-muted-foreground">{t('upload.schedule.waiting')}</p>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={schedule.isPending} onClick={() => send(null)}>
            {t('upload.schedule.startNow')}
          </Button>
          {draft === null ? (
            <Button size="sm" variant="outline" onClick={() => setDraft(localInputValue(parseApiDate(job.scheduled_at!)))}>
              {t('upload.schedule.change')}
            </Button>
          ) : (
            <>
              <Input
                type="datetime-local"
                aria-label={t('upload.schedule.at')}
                className="h-8 w-fit"
                value={draft}
                min={localInputValue(new Date())}
                onChange={(e) => setDraft(e.target.value)}
              />
              <Button size="sm" variant="outline" disabled={schedule.isPending || fromLocalInput(draft) === null}
                      onClick={() => send(fromLocalInput(draft))}>
                {t('common.save')}
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

// Lo stage del worker (nazgarr/upload/execute.py): "hashing", "screenshots",
// "tracker:<label>".
function stageLabel(stage: string | null) {
  if (!stage) return null
  if (stage.startsWith('tracker:')) return t('upload.progress.tracker', { tracker: stage.slice('tracker:'.length) })
  return t(`upload.progress.${stage}`)
}

export function ProgressStep({ job }: { job: UploadJob }) {
  if (job.status === 'queued' && isFuture(job.scheduled_at)) return <ScheduledStep job={job} />
  const total = job.progress_total
  const pct = total ? Math.round((100 * (job.progress_done ?? 0)) / total) : null
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <LoaderCircleIcon className="size-4 animate-spin text-primary" />
          {job.status === 'queued'
            ? t('upload.working.queued')
            : (stageLabel(job.stage) ?? t(`upload.working.${job.status}`))}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <ExecutionSteps steps={executionSteps(job)} />
        {pct !== null && (
          <div className="grid gap-1">
            <Progress value={pct} />
            <span className="text-xs text-muted-foreground tabular-nums">
              {job.progress_done}/{total} ({pct}%)
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
