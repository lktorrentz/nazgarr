import { CheckIcon, ExternalLinkIcon, LoaderCircleIcon, ScanSearchIcon, TriangleAlertIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { toast } from 'sonner'

import { useVerifyTarget, type UploadDupe as Dupe, type UploadJob, type UploadTarget } from '@/api/hooks/uploads'
import { UploadStatusBadge } from '@/components/upload/UploadStatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { t, uiLocale } from '@/lib/i18n'
import { formatBytes } from '@/lib/library-filters'
import { dupeUrl, needsResurrectionClaim } from '@/lib/upload'
import { cn } from '@/lib/utils'
import { safeHref } from '@/lib/safeUrl'

const VERDICT_STYLE: Record<Dupe['verdict'], string> = {
  identical: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  same_slot: 'border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300',
  trump: 'border-violet-500/40 bg-violet-500/15 text-violet-700 dark:text-violet-300',
  possible: 'border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300',
  different: 'border-zinc-400/40 bg-zinc-400/15 text-zinc-600 dark:text-zinc-300',
}

// Il nome di un dupe: sul telefono va a capo (fino a tre righe), da sm si tronca.
const DUPE_NAME = 'font-mono max-sm:line-clamp-3 max-sm:break-all sm:truncate'

const ACTION_STYLE: Record<string, string> = {
  upload: 'border-sky-500/40 bg-sky-500/15 text-sky-700 dark:text-sky-300',
  reseed: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  skip: 'border-zinc-400/40 bg-zinc-400/15 text-zinc-600 dark:text-zinc-300',
}

export function ActionBadge({ action }: { action: string }) {
  return (
    <Badge variant="outline" className={ACTION_STYLE[action]}>
      {t(`upload.action.${action}`)}
    </Badge>
  )
}

function verificationText(dupe: Dupe): string | null {
  const v = dupe.verification
  if (!v) return null
  if (v.status === 'passed') return t('upload.dupes.verifyPassed', { pieces: v.pieces })
  if (v.status === 'failed') return t('upload.dupes.verifyFailed', { reason: v.reason })
  return t('upload.dupes.verifyError', { reason: v.reason })
}

function Verification({ dupe }: { dupe: Dupe }) {
  const text = verificationText(dupe)
  if (!text) return null
  const ok = dupe.verification?.status === 'passed'
  return <span className={ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>{text}</span>
}

function signedPercent(delta: number) {
  const value = Math.round(delta * 100)
  return `${value > 0 ? '+' : ''}${value}%`
}

// Motivi del verdetto, tratti ignoti e un torrent senza seeder (nazgarr/upload/dupes.py).
function dupeNotes(dupe: Dupe): string[] {
  const notes = dupe.reasons.map((reason) =>
    reason === 'size_gap' && dupe.size_delta != null
      ? t('upload.dupes.reason.size_gap', { delta: signedPercent(dupe.size_delta) })
      : t(`upload.dupes.reason.${reason}`),
  )
  const close = dupe.verdict !== 'different' && dupe.verdict !== 'identical'
  if (close && dupe.unknown.length > 0) {
    notes.push(t('upload.dupes.unknown', { traits: dupe.unknown.map((trait) => t(`upload.dupes.trait.${trait}`)).join(', ') }))
  }
  if (dupe.seeders === 0 && dupe.verdict !== 'different') {
    notes.push(t(dupe.verdict === 'identical' ? 'upload.dupes.deadIdentical' : 'upload.dupes.dead'))
  }
  return notes
}

// Motivi e verifica in una riga: per il title quando su desktop si tronca.
function dupeDetails(dupe: Dupe): string {
  return [...dupeNotes(dupe), verificationText(dupe)].filter(Boolean).join(' · ')
}

function languageName(code: string) {
  try {
    return new Intl.DisplayNames([uiLocale()], { type: 'language' }).of(code) ?? code
  } catch {
    return code
  }
}

function SectionLabel({ children }: { children: ReactNode }) {
  return <p className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{children}</p>
}

// Un tracker del job: il dupe check con il verdetto di ogni risultato (una
// riga ciascuno: verdetto, nome e motivi, dimensione, link e azione), il
// full hash check sulle release "identical" e (children) la decisione.
export function TrackerCheckCard({
  job,
  target,
  children,
  claimed = false,
  onClaimedChange,
}: {
  job: UploadJob
  target: UploadTarget
  children?: ReactNode
  // La resurrection di un identico senza seeder segnata come rivendicata (DecisionStep).
  claimed?: boolean
  onClaimedChange?: (claimed: boolean) => void
}) {
  const verify = useVerifyTarget(job.id)
  const dupes = target.dupes
  const verifying = target.status === 'verifying'
  const canVerify = job.status === 'awaiting_decision' && target.status === 'awaiting_decision'
  const seeding = job.analysis?.seeding_here?.[String(target.id)]
  // Il tracker aveva più risultati di quelli letti (nazgarr/upload/analysis.py):
  // un dupe oltre il limite non si vede.
  const truncated = job.analysis?.dupes_truncated?.[String(target.id)]
  // La lingua del tracker nell'audio del file (nazgarr/upload/naming.py audio_language_check).
  const language = job.analysis?.languages?.[String(target.tracker_id)]
  const hasPossible = dupes.some((dupe) => dupe.verdict === 'possible')
  const hasTrump = dupes.some((dupe) => dupe.verdict === 'trump')

  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <div className="grid min-w-0 gap-0.5">
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            {target.tracker_label}
            <UploadStatusBadge status={target.status} />
          </CardTitle>
          <span className="text-xs text-muted-foreground">
            {target.torrent_client_label
              ? t('upload.seedsOn', { client: target.torrent_client_label })
              : t('upload.noClient')}
          </span>
        </div>
        {target.suggested_action && (
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            {t('upload.dupes.suggested')}
            <ActionBadge action={target.suggested_action} />
          </span>
        )}
      </CardHeader>
      <CardContent className="grid min-w-0 gap-5">
        {language && language.status !== 'present' && (
          <p className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
            <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
            {t(`upload.language.${language.status}`, { language: languageName(language.language) })}
          </p>
        )}
        <div className="grid min-w-0 gap-2">
          <SectionLabel>{t('upload.dupes.onTracker')}</SectionLabel>
          {seeding && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              {t('upload.dupes.seedingHere', { client: seeding.client, torrent: seeding.name })}
            </p>
          )}
          {target.error_message === 'dupe_check_failed' && (
            <p className="text-sm text-amber-600 dark:text-amber-400">{t('upload.dupes.checkFailed')}</p>
          )}
          {truncated != null && (
            <p className="flex items-start gap-2 text-sm text-amber-600 dark:text-amber-400">
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              {t('upload.dupes.truncated', { count: truncated })}
            </p>
          )}
          {target.error_message !== 'dupe_check_failed' && dupes.length === 0 && target.status !== 'checking' && (
            <p className="text-sm text-muted-foreground">{t('upload.dupes.none')}</p>
          )}
          {dupes.length > 0 && (
            <ul className="grid min-w-0 divide-y rounded-md border">
              {dupes.map((dupe) => {
                const url = dupeUrl(target, dupe.torrent_id_remote)
                return (
                  <li
                    key={dupe.torrent_id_remote}
                    className={cn(
                      // Sul telefono le azioni vanno su una riga loro: accanto lasciavano
                      // al nome pochi pixel. Da sm tre colonne, come prima.
                      'grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 px-3 py-2 text-xs sm:grid-cols-[auto_minmax(0,1fr)_auto]',
                      dupe.verdict === 'different' && 'opacity-70',
                    )}
                  >
                    <Badge variant="outline" className={cn('justify-self-start', VERDICT_STYLE[dupe.verdict])}>
                      {t(`upload.dupes.verdict.${dupe.verdict}`)}
                      {dupe.score != null && <span className="tabular-nums opacity-80">{t('upload.dupes.score', { score: dupe.score })}</span>}
                    </Badge>
                    <div className="grid min-w-0">
                      {url ? (
                        <a href={safeHref(url)} target="_blank" rel="noreferrer" className={cn(DUPE_NAME, 'hover:underline')} title={dupe.name}>
                          {dupe.name}
                        </a>
                      ) : (
                        <span className={DUPE_NAME} title={dupe.name}>{dupe.name}</span>
                      )}
                      {(dupeNotes(dupe).length > 0 || dupe.verification) && (
                        <span className="text-muted-foreground max-sm:break-words sm:truncate" title={dupeDetails(dupe)}>
                          {dupeNotes(dupe).join(' · ')}
                          {dupeNotes(dupe).length > 0 && dupe.verification && ' · '}
                          <Verification dupe={dupe} />
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 max-sm:col-span-full max-sm:justify-self-end">
                      {dupe.verdict === 'identical' && !dupe.verification && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 pointer-coarse:h-9"
                          disabled={!canVerify || verify.isPending}
                          onClick={() =>
                            verify.mutate(
                              { targetId: target.id, torrentIdRemote: dupe.torrent_id_remote },
                              { onError: (error) => toast.error(error.message) },
                            )
                          }
                        >
                          {verifying ? (
                            <LoaderCircleIcon className="size-3.5 animate-spin" />
                          ) : (
                            <ScanSearchIcon className="size-3.5" />
                          )}
                          {verifying ? t('upload.dupes.verifying') : t('upload.dupes.verify')}
                        </Button>
                      )}
                      {dupe.seeders != null && (
                        <span className={cn('tabular-nums', dupe.seeders === 0 ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground')}>
                          {t('upload.dupes.seeders', { count: dupe.seeders })}
                        </span>
                      )}
                      {/* Non tutti i tracker danno la dimensione: senza, niente "null B". */}
                      {dupe.size_bytes != null && (
                        <span className="text-muted-foreground tabular-nums">{formatBytes(dupe.size_bytes)}</span>
                      )}
                    </div>
                    {/* Con la sessione web del tracker il claim lo fa Nazgarr prima del
                        reseed (nazgarr/upload/execute.py::_claim_resurrection, 2026-11-10). */}
                    {onClaimedChange && needsResurrectionClaim(dupe) && target.auto_resurrection && (
                      <p className="col-span-full flex items-start gap-2 rounded-md border border-sky-500/40 bg-sky-500/10 p-2">
                        <CheckIcon className="mt-0.5 size-3.5 shrink-0 text-sky-500" />
                        {t('upload.dupes.resurrectionAuto')}
                      </p>
                    )}
                    {onClaimedChange && needsResurrectionClaim(dupe) && !target.auto_resurrection && (
                      <div className="col-span-full grid gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2">
                        <p className="flex items-start gap-2">
                          <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                          {claimed ? t('upload.dupes.resurrectionDone') : t('upload.dupes.resurrection')}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {url && (
                            <a
                              href={safeHref(url)}
                              target="_blank"
                              rel="noreferrer"
                              className={buttonVariants({ variant: 'outline', size: 'sm', className: 'h-7 pointer-coarse:h-9' })}
                            >
                              <ExternalLinkIcon className="size-3.5" />
                              {t('upload.dupes.resurrectionOpen')}
                            </a>
                          )}
                          <Button
                            size="sm"
                            variant={claimed ? 'secondary' : 'outline'}
                            className="h-7 pointer-coarse:h-9"
                            aria-pressed={claimed}
                            onClick={() => onClaimedChange(!claimed)}
                          >
                            <CheckIcon className="size-3.5" />
                            {t('upload.dupes.resurrectionClaimed')}
                          </Button>
                        </div>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          {hasPossible && (
            <p className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-400">
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              {t('upload.dupes.possibleHint')}
            </p>
          )}
          {hasTrump && <p className="text-xs text-violet-700 dark:text-violet-300">{t('upload.dupes.trumpHint')}</p>}
        </div>
        {children && (
          <div className="grid min-w-0 gap-2">
            <SectionLabel>{t('upload.decision.yourDecision')}</SectionLabel>
            {children}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
