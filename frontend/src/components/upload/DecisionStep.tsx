import { TriangleAlertIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { useApproveUpload, type UploadJob } from '@/api/hooks/uploads'
import { Masonry } from '@/components/Masonry'
import { AnalysisSummary } from '@/components/upload/AnalysisSummary'
import { DecisionSummary } from '@/components/upload/DecisionSummary'
import { FileNamesCard } from '@/components/upload/FileNamesCard'
import { MatchSummaryCard } from '@/components/upload/MatchSummaryCard'
import { MediaInfoPreview } from '@/components/upload/MediaInfoPreview'
import { OverridesPanel } from '@/components/upload/OverridesPanel'
import { PackMixedCard } from '@/components/upload/PackMixedCard'
import { ScheduleField } from '@/components/upload/ScheduleField'
import { TargetDecisionForm } from '@/components/upload/TargetDecisionForm'
import { ActionBadge, TrackerCheckCard } from '@/components/upload/TrackerCheckCard'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { t } from '@/lib/i18n'
import type { MediaInfoSummary } from '@/lib/mediainfo'
import { packMixed, packMixedConfirmed } from '@/lib/pack'
import { fromLocalInput } from '@/lib/schedule'
import { effectiveDraft, sourceMissing, type TargetDraft } from '@/lib/upload'

// Secondo punto di approvazione (docs/SPEC.md §9): cosa ha trovato
// l'analisi, i valori rilevati da correggere e, per ogni tracker, dupe
// check e decisione. Approvare è la conferma finale: da lì il worker fa
// tutto da solo, quindi il dialogo riassume cosa succederà.
export function DecisionStep({ job }: { job: UploadJob }) {
  const [edits, setEdits] = useState<Record<number, TargetDraft>>({})
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [overridesOpen, setOverridesOpen] = useState(false)
  // Quando parte: null = appena tocca a lui, se no il valore del campo data e ora.
  const [when, setWhen] = useState<string | null>(null)
  const approve = useApproveUpload(job.id)
  const missingSource = sourceMissing(job)
  // Dall'avviso sopra il nome o dalla conferma: apre i valori rilevati e ci
  // porta lì, col campo Sorgente.
  function goToSource() {
    setConfirmOpen(false)
    setOverridesOpen(true)
    // Dopo che il pannello si è aperto: prima il campo non c'è ancora.
    window.setTimeout(() => {
      document.getElementById('upload-overrides')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      document.getElementById('override-source')?.focus({ preventScroll: true })
    }, 50)
  }

  const drafts = job.targets.map((target) => ({ target, draft: effectiveDraft(edits[target.id], target) }))
  const busy = job.targets.some((target) => target.status !== 'awaiting_decision')
  // Un pack misto non confermato ferma gli upload, non i reseed (nazgarr/upload/decision.py).
  const blocked =
    packMixed(job) && !packMixedConfirmed(job) && drafts.some(({ draft }) => draft.action === 'upload')
      ? t('errors.upload_pack_mixed_unconfirmed', { fields: Object.keys(packMixed(job) ?? {}).map((f) => t(`pack.mixedField.${f}`)).join(', ') })
      : null

  function submit() {
    approve.mutate({
      scheduled_at: when !== null ? fromLocalInput(when) : null,
      targets: drafts.map(({ target, draft }) => ({
        target_id: target.id,
        action: draft.action,
        name: draft.action === 'upload' ? draft.name : null,
        flags: draft.action === 'upload' ? { ...draft.flags, freeleech: draft.freeleech } : null,
        category_id: draft.action === 'upload' ? draft.category_id : null,
        type_id: draft.action === 'upload' ? draft.type_id : null,
        resolution_id: draft.action === 'upload' ? draft.resolution_id : null,
        reseed_torrent_id: draft.action === 'reseed' ? draft.reseed_torrent_id : null,
        ...(draft.action !== 'skip' ? { client_category: draft.client_category, client_tags: draft.client_tags } : {}),
      })),
    }, {
      onSuccess: () => setConfirmOpen(false),
      onError: (error) => toast.error(t('upload.decision.approveFailed', { message: error.message })),
    })
  }

  return (
    <div className="grid min-w-0 gap-4 [&>*]:min-w-0">
      <PackMixedCard job={job} />
      {/* Masonry: ogni scheda nella colonna più corta, così un MediaInfo
          lungo non spinge l'esito dell'analisi sotto di sé. */}
      <Masonry>
        <MatchSummaryCard job={job} />
        <MediaInfoPreview
          summary={((job.analysis as Record<string, unknown> | null)?.mediainfo ?? null) as MediaInfoSummary | null}
          fullText={job.mediainfo_text}
        />
        <OverridesPanel key={JSON.stringify(job.overrides)} job={job} open={overridesOpen} onOpenChange={setOverridesOpen} />
        <FileNamesCard job={job} />
        <AnalysisSummary job={job} />
      </Masonry>
      {drafts.map(({ target, draft }) => (
        <TrackerCheckCard key={target.id} job={job} target={target}>
          <TargetDecisionForm
            target={target}
            draft={draft}
            disabled={target.status !== 'awaiting_decision'}
            onChange={(next) => setEdits((prev) => ({ ...prev, [target.id]: next }))}
            missingSource={missingSource}
            onSetSource={goToSource}
          />
        </TrackerCheckCard>
      ))}
      <DecisionSummary drafts={drafts} busy={busy} blocked={blocked} onApprove={() => setConfirmOpen(true)} />

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('upload.decision.confirmTitle')}</DialogTitle>
            <DialogDescription>{t('upload.decision.confirmDescription')}</DialogDescription>
          </DialogHeader>
          <ul className="grid gap-2 text-sm">
            {drafts.map(({ target, draft }) => (
              <li key={target.id} className="grid gap-0.5">
                <span className="flex items-center gap-2 font-medium">
                  {target.tracker_label}
                  <ActionBadge action={draft.action} />
                </span>
                {draft.action === 'upload' && <span className="font-mono text-xs break-all">{draft.name}</span>}
              </li>
            ))}
          </ul>
          {/* Un avviso, non un blocco: la sorgente può essere già scritta a mano nel nome. */}
          {missingSource && drafts.some(({ draft }) => draft.action === 'upload') && (
            <div className="grid gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
              <p className="flex items-start gap-2">
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-500" />
                {t('upload.decision.confirmWithoutSource')}
              </p>
              <Button size="sm" variant="outline" className="w-fit" onClick={goToSource}>
                {t('upload.decision.addSource')}
              </Button>
            </div>
          )}
          {drafts.some(({ draft }) => draft.action !== 'skip') && <ScheduleField value={when} onChange={setWhen} />}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button disabled={approve.isPending || (when !== null && fromLocalInput(when) === null)} onClick={submit}>
              {when !== null
                ? t('upload.decision.confirmScheduled')
                : missingSource && drafts.some(({ draft }) => draft.action === 'upload')
                  ? t('upload.decision.confirmAnyway')
                  : t('upload.decision.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
