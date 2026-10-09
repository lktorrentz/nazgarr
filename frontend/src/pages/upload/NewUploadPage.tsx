import { CheckIcon, FileVideoIcon, FolderIcon, FolderSearchIcon, PackageIcon, TriangleAlertIcon } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { useCreateUpload, useImageHostStatus, useUploadTrackers } from '@/api/hooks/uploads'
import { ForcedIdFields } from '@/components/upload/ForcedIdFields'
import { SourcePickerSheet, type UploadSource } from '@/components/upload/SourcePickerSheet'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { t } from '@/lib/i18n'
import { EMPTY_IDS, parseNewUploadParams, toForcedIds } from '@/lib/upload'
import { readPackState, type PackState } from '@/lib/pack'
import { cn } from '@/lib/utils'

// Prima di iniziare: senza un host di immagini utilizzabile (acceso e con la
// sua API key) un upload fallirebbe agli screenshot.
function ImageHostWarning() {
  const { data } = useImageHostStatus()
  if (!data || data.usable.length > 0) return null
  return (
    <div role="alert" className="flex gap-3 rounded-lg border border-red-500/40 bg-red-500/10 p-4 text-sm">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-red-600 dark:text-red-400" />
      <div className="grid gap-1">
        <p className="font-medium">{t('upload.imageHostsNoneTitle')}</p>
        <p className="text-muted-foreground">{t('upload.imageHostsNone')}</p>
        <Link to="/config?tab=images" className="w-fit font-medium text-primary underline-offset-4 hover:underline">
          {t('upload.imageHostsSettings')}
        </Link>
      </div>
    </div>
  )
}

export function NewUploadPage() {
  const navigate = useNavigate()
  const { data: trackers, isPending: trackersPending } = useUploadTrackers()
  const create = useCreateUpload()
  const [pickerOpen, setPickerOpen] = useState(false)
  // Arrivando dalla vista poster: sorgente e TMDB già scelti (?disk=&path=&tmdb=).
  const [params] = useSearchParams()
  const [initial] = useState(() => parseNewUploadParams(params))
  const [source, setSource] = useState<UploadSource | null>(initial.source)
  // Episodi scelti a mano per un pack (nazgarr/upload/pack.py), dalla
  // libreria o dalla vista dei torrent.
  const location = useLocation()
  const [pack, setPack] = useState<PackState | null>(() => readPackState(location.state))
  const [ids, setIds] = useState({ ...EMPTY_IDS, tmdb: initial.tmdb })
  // null = scelta non ancora toccata: tutti i tracker con un profilo di upload.
  const [trackerChoice, setTrackerChoice] = useState<Set<number> | null>(
    () => (initial.trackers ? new Set(initial.trackers) : null),
  )
  const selectedTrackers = trackerChoice ?? (trackers ? new Set(trackers.map((tr) => tr.id)) : null)

  // Freeleech scelto per tracker (solo per chi lo concede): undefined = il
  // default del profilo.
  const [freeleech, setFreeleech] = useState<Record<number, number>>({})
  const freeleechOf = (id: number) =>
    freeleech[id] ?? trackers?.find((tr) => tr.id === id)?.default_freeleech ?? 0

  const toggleTracker = (id: number) =>
    setTrackerChoice(() => {
      const next = new Set(selectedTrackers ?? [])
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const canSubmit = (pack !== null || source !== null) && (selectedTrackers?.size ?? 0) > 0 && !create.isPending

  function submit() {
    if ((!source && !pack) || !selectedTrackers) return
    create.mutate(
      {
        ...(pack
          ? { disk_id: pack.diskId, files: pack.files }
          : { disk_id: source!.diskId, relative_path: source!.relativePath }),
        tracker_ids: [...selectedTrackers],
        forced_ids: toForcedIds(ids),
        tracker_choices: Object.fromEntries(
          [...selectedTrackers]
            .filter((id) => freeleechOf(id) > 0)
            .map((id) => [id, { freeleech: freeleechOf(id) }]),
        ),
      },
      {
        onSuccess: (job) => navigate(`/upload/${job.id}`),
        onError: (error) => toast.error(t('upload.createFailed', { message: error.message })),
      },
    )
  }

  // min-w-0 su ogni figlio delle griglie: un percorso lungo senza spazi
  // (troncato) le allargherebbe fino alla sua larghezza, e Sfoglia e Avvia
  // finivano tagliati fuori dalla scheda.
  return (
    <div className="grid min-w-0 gap-4 [&>*]:min-w-0">
      <ImageHostWarning />
      <Card>
        <CardHeader>
          <CardTitle>{t('upload.newUpload')}</CardTitle>
          <CardDescription>{t('upload.newUploadDescription')}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 [&>*]:min-w-0">
          {pack ? (
            <div className="grid gap-1.5">
              <Label>{t('upload.source')}</Label>
              <div className="grid gap-1 rounded-md border bg-muted/30 p-2.5 font-mono text-xs">
                <span className="flex items-center gap-1.5 font-sans text-sm font-medium">
                  <PackageIcon className="size-4 shrink-0 text-primary" />
                  {t('pack.source', { count: pack.files.length })}
                </span>
                {pack.files.map((file) => (
                  <span key={file} className="flex min-w-0 items-center gap-1.5 pl-5 break-all text-muted-foreground">
                    <FileVideoIcon className="size-3.5 shrink-0" />
                    {file}
                  </span>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">{t('pack.sourceHelp')}</p>
              <Button variant="link" size="sm" className="h-auto w-fit p-0" onClick={() => setPack(null)}>
                {t('pack.change')}
              </Button>
            </div>
          ) : (
          <div className="grid gap-1.5 [&>*]:min-w-0">
            <Label>{t('upload.source')}</Label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border bg-transparent px-3 text-left font-mono text-xs shadow-xs hover:bg-muted"
              >
                {source ? (
                  source.isDir ? (
                    <FolderIcon className="size-4 shrink-0 text-primary" />
                  ) : (
                    <FileVideoIcon className="size-4 shrink-0 text-muted-foreground" />
                  )
                ) : null}
                {/* Si tronca l'inizio del percorso, non il nome del file in fondo
                    (direzione rtl, col testo isolato in un bdi, come nel selettore). */}
                <span
                  className={cn('min-w-0 truncate [direction:rtl]', !source && 'text-muted-foreground')}
                  title={source?.relativePath}
                >
                  <bdi>{source ? source.relativePath : t('upload.sourcePlaceholder')}</bdi>
                </span>
              </button>
              <Button variant="outline" onClick={() => setPickerOpen(true)}>
                <FolderSearchIcon className="size-4" />
                {t('upload.browse')}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t('upload.sourceHelp')}</p>
          </div>
          )}

          <div className="grid gap-2">
            <div>
              <Label>{t('upload.forcedIds')}</Label>
              <p className="text-xs text-muted-foreground">{t('upload.forcedIdsHelp')}</p>
            </div>
            <ForcedIdFields ids={ids} onChange={setIds} />
          </div>

          <div className="grid gap-2">
            <div>
              <Label>{t('upload.trackers')}</Label>
              <p className="text-xs text-muted-foreground">{t('upload.trackersHelp')}</p>
            </div>
            {trackersPending && <p className="text-sm text-muted-foreground">{t('common.loading')}</p>}
            {trackers?.length === 0 && <p className="text-sm text-muted-foreground">{t('upload.noUploadTrackers')}</p>}
            <div className="flex flex-wrap gap-2">
              {trackers?.map((tracker) => {
                const active = selectedTrackers?.has(tracker.id) ?? false
                // Il freeleech sta fuori dal pulsante del tracker (prima erano
                // span cliccabili dentro un button, minuscoli al tocco): stessa
                // scheda, pulsanti veri da 32px come nella decisione.
                return (
                  <div
                    key={tracker.id}
                    className={cn(
                      'grid gap-2 rounded-md border px-3 py-2 text-sm transition',
                      active ? 'border-primary bg-primary/10' : 'text-muted-foreground hover:bg-muted',
                    )}
                  >
                    <button
                      type="button"
                      aria-pressed={active}
                      onClick={() => toggleTracker(tracker.id)}
                      className="flex items-center gap-2 text-left"
                    >
                      <span
                        className={cn(
                          'flex size-4 items-center justify-center rounded-sm border',
                          active && 'border-primary bg-primary text-primary-foreground',
                        )}
                      >
                        {active && <CheckIcon className="size-3" />}
                      </span>
                      <span className="grid">
                        <span className="font-medium text-foreground">{tracker.label}</span>
                        <span className="text-xs text-muted-foreground">
                          {tracker.torrent_client_label
                            ? t('upload.seedsOn', { client: tracker.torrent_client_label })
                            : t('upload.noClient')}
                        </span>
                      </span>
                    </button>
                    {active && tracker.freeleech_options.length > 0 && (
                      <div
                        role="group"
                        aria-label={t('upload.decision.freeleech')}
                        className="inline-flex w-fit flex-wrap overflow-hidden rounded-md border bg-background"
                      >
                        {[0, ...tracker.freeleech_options].map((value) => (
                          <button
                            key={value}
                            type="button"
                            aria-pressed={freeleechOf(tracker.id) === value}
                            onClick={() => setFreeleech((prev) => ({ ...prev, [tracker.id]: value }))}
                            className={cn(
                              'h-8 border-r px-3 text-xs tabular-nums last:border-r-0',
                              freeleechOf(tracker.id) === value
                                ? 'bg-primary/15 text-primary'
                                : 'text-muted-foreground hover:bg-muted',
                            )}
                          >
                            {value === 0 ? t('upload.noFreeleech') : `FL ${value}%`}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => navigate('/upload')}>
              {t('common.cancel')}
            </Button>
            <Button disabled={!canSubmit} onClick={submit}>
              {create.isPending ? t('upload.starting') : t('upload.start')}
            </Button>
          </div>
        </CardContent>
      </Card>
      <SourcePickerSheet open={pickerOpen} onOpenChange={setPickerOpen} initial={source} onSelect={setSource} />
    </div>
  )
}
