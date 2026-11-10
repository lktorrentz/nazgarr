// Pezzi condivisi dalle pagine del flusso di upload v2 (docs/SPEC.md §9).

import type { UploadDupe } from '@/api/hooks/uploads'
import { t } from '@/lib/i18n'

export const EMPTY_IDS = { tmdb: '', imdb: '', tvdb: '', mal: '' }
export type ForcedIdsDraft = typeof EMPTY_IDS
export type IdKey = keyof ForcedIdsDraft

export const ID_FIELDS: { key: IdKey; label: string; placeholder: string }[] = [
  { key: 'tmdb', label: 'TMDB', placeholder: 'movie/603, tv/1399, URL' },
  { key: 'imdb', label: 'IMDB', placeholder: 'tt0133093' },
  { key: 'tvdb', label: 'TVDB', placeholder: '121361' },
  { key: 'mal', label: 'MAL', placeholder: '5114' },
]

export function toForcedIds(ids: ForcedIdsDraft) {
  const number = (value: string) => (/^\d+$/.test(value.trim()) ? Number(value.trim()) : undefined)
  return {
    tmdb: ids.tmdb.trim() || undefined,
    imdb: ids.imdb.trim() || undefined,
    tvdb: number(ids.tvdb),
    mal: number(ids.mal),
  }
}

export function fromForcedIds(forced: Record<string, unknown> | undefined): ForcedIdsDraft {
  const text = (value: unknown) => (value == null ? '' : String(value))
  return { tmdb: text(forced?.tmdb), imdb: text(forced?.imdb), tvdb: text(forced?.tvdb), mal: text(forced?.mal) }
}

export type UploadKind = 'movie' | 'episode' | 'season_pack' | 'complete_pack'
export const TV_KINDS: UploadKind[] = ['episode', 'season_pack', 'complete_pack']

// Episodi attesi e mancanti di una stagione, dal confronto fra i file
// trovati nella sorgente e il numero di episodi che TMDB conosce.
export function missingEpisodes(found: number[], expected: number): number[] {
  const have = new Set(found)
  const missing: number[] = []
  for (let episode = 1; episode <= expected; episode++) if (!have.has(episode)) missing.push(episode)
  return missing
}

// Il backend scrive code + params (nazgarr/upload/jobs.py log_event): la frase
// è qui, tradotta. Un codice d'errore senza una frase dedicata passa da
// errors.<code>, lo stesso dizionario degli errori delle API.
// Un parametro "error" che è a sua volta un codice (upload_failed, reseed_failed:
// nazgarr/upload/execute.py::_run_target) diventa la sua frase, con gli stessi parametri.
export function eventMessage(event: { code: string; params: Record<string, unknown> }) {
  const params = { ...event.params }
  if (typeof params.error === 'string') {
    const errorKey = `errors.${params.error}`
    const translated = t(errorKey, params)
    if (translated !== errorKey) params.error = translated
  }
  const key = `upload.event.${event.code}`
  const message = t(key, params)
  return message === key ? t(`errors.${event.code}`, params) : message
}

// Bozza della decisione per un tracker, al secondo punto di approvazione.
// touched: i campi che l'utente ha cambiato a mano; gli altri seguono i
// valori proposti dal backend, che cambiano quando si salvano gli override.
export interface TargetDraft {
  action: 'upload' | 'reseed' | 'skip'
  name: string
  category_id: number | null
  type_id: number | null
  resolution_id: number | null
  flags: Record<string, boolean>
  freeleech: number  // percentuale, 0 = nessuno
  reseed_torrent_id: string | null
  // Nel client del tracker: categoria (null = nessuna) e tag separati da virgola.
  client_category: string | null
  client_tags: string
  // Il torrent da rimettere in seed ha passato il full hash check: senza, niente reseed.
  reseed_verified: boolean
  touched: (keyof Omit<TargetDraft, 'touched' | 'reseed_verified'>)[]
}

function verified(dupes: Pick<UploadDupe, 'torrent_id_remote' | 'verification'>[], torrentId: string | null) {
  return dupes.find((d) => d.torrent_id_remote === torrentId)?.verification?.status === 'passed'
}

interface ClientDefaults {
  category?: string | null
  tags_upload?: string | null
  tags_reseed?: string | null
}

// I tag proposti seguono l'azione: quelli per gli upload o per i reseed.
function defaultTags(defaults: ClientDefaults | undefined, action: TargetDraft['action']) {
  if (action === 'upload') return defaults?.tags_upload ?? ''
  if (action === 'reseed') return defaults?.tags_reseed ?? ''
  return ''
}

interface DraftSource {
  suggested_action: string | null
  proposed_name: string | null
  category_id: number | null
  type_id: number | null
  resolution_id: number | null
  flags: Record<string, unknown>
  reseed_torrent_id: string | null
  dupes: Pick<UploadDupe, 'torrent_id_remote' | 'verdict' | 'verification'>[]
  client_category?: string | null
  client_tags?: string | null
  client_defaults?: ClientDefaults
}

export function initialDraft(target: DraftSource): TargetDraft {
  const identical = target.dupes.filter((d) => d.verdict === 'identical')
  const suggested = target.suggested_action
  const action = suggested === 'reseed' || suggested === 'skip' ? suggested : 'upload'
  return {
    action,
    client_category: target.client_category ?? target.client_defaults?.category ?? null,
    client_tags: target.client_tags ?? defaultTags(target.client_defaults, action),
    name: target.proposed_name ?? '',
    category_id: target.category_id,
    type_id: target.type_id,
    resolution_id: target.resolution_id,
    flags: Object.fromEntries(
      Object.entries(target.flags)
        .filter(([k]) => k !== 'freeleech')
        .map(([k, v]) => [k, v === true]),
    ),
    freeleech: typeof target.flags.freeleech === 'number' ? target.flags.freeleech : 0,
    reseed_torrent_id: target.reseed_torrent_id ?? identical[0]?.torrent_id_remote ?? null,
    reseed_verified: verified(target.dupes, target.reseed_torrent_id ?? identical[0]?.torrent_id_remote ?? null),
    touched: [],
  }
}

/** La bozza da mostrare: i campi toccati dall'utente, il resto proposto. */
export function effectiveDraft(edits: Partial<TargetDraft> | undefined, target: DraftSource): TargetDraft {
  const fresh = initialDraft(target)
  if (!edits) return fresh
  const out: TargetDraft = { ...fresh, touched: edits.touched ?? [] }
  for (const key of out.touched) (out as unknown as Record<string, unknown>)[key] = edits[key]
  out.reseed_verified = verified(target.dupes, out.reseed_torrent_id)
  // Azione cambiata a mano, tag no: i tag proposti per la nuova azione.
  if (!out.touched.includes('client_tags') && target.client_tags == null) {
    out.client_tags = defaultTags(target.client_defaults, out.action)
  }
  return out
}

export function editDraft(
  draft: TargetDraft,
  patch: Partial<Omit<TargetDraft, 'touched' | 'reseed_verified'>>,
): TargetDraft {
  const touched = new Set(draft.touched)
  for (const key of Object.keys(patch)) touched.add(key as keyof Omit<TargetDraft, 'touched' | 'reseed_verified'>)
  return { ...draft, ...patch, touched: [...touched] }
}

export function draftProblem(draft: TargetDraft): string | null {
  if (draft.action === 'upload') {
    if (!draft.name.trim()) return 'upload.decision.problem.name'
    if (draft.category_id == null || draft.type_id == null || draft.resolution_id == null) {
      return 'upload.decision.problem.ids'
    }
  }
  if (draft.action === 'reseed' && !draft.reseed_torrent_id) return 'upload.decision.problem.reseed'
  if (draft.action === 'reseed' && !draft.reseed_verified) return 'upload.decision.problem.verify'
  return null
}

// Pagina di un torrent sul tracker (UNIT3D: /torrents/<id>).
export function dupeUrl(target: { tracker_base_url: string | null }, torrentId: string) {
  return target.tracker_base_url ? `${target.tracker_base_url}/torrents/${torrentId}` : null
}

// Un identico senza seeder, verificato: il reseed lo resuscita, e la
// resurrection va rivendicata sul tracker prima che parta (decisione
// dell'utente, 2026-11-07). Un avviso, mai un blocco: la conferma lo richiede.
export function needsResurrectionClaim(
  dupe: { verdict: string; seeders?: number | null; verification?: { status: string } | null } | undefined,
): boolean {
  return dupe?.verdict === 'identical' && dupe.seeders === 0 && dupe.verification?.status === 'passed'
}

// Link alla pagina di nuovo upload con sorgente e id già compilati (dalla
// vista poster): il match con quel TMDB è immediato, e se il tracker ha già
// la release identica il flusso stesso propone il reseed.
export function newUploadLink(
  source: { diskId: number; path: string; isDir: boolean },
  tmdb?: string,
  trackerIds?: number[],
) {
  const params = new URLSearchParams({ disk: String(source.diskId), path: source.path })
  if (source.isDir) params.set('dir', '1')
  if (tmdb) params.set('tmdb', tmdb)
  // Solo questi tracker selezionati (es. "Upload to ITT" dalla panoramica).
  if (trackerIds?.length) params.set('trackers', trackerIds.join(','))
  return `/upload/new?${params}`
}

export function parseNewUploadParams(params: URLSearchParams) {
  const disk = Number(params.get('disk'))
  const path = params.get('path')
  const source = disk && path ? { diskId: disk, relativePath: path, isDir: params.get('dir') === '1' } : null
  const trackers = (params.get('trackers') ?? '')
    .split(',')
    .map(Number)
    .filter((id) => Number.isInteger(id) && id > 0)
  return { source, tmdb: params.get('tmdb') ?? '', trackers: trackers.length ? trackers : null }
}

// La cartella che contiene tutti i percorsi dati (sullo stesso disco), o null.
export function commonFolder(files: { disk_id: number; relative_path: string }[]): { diskId: number; path: string } | null {
  if (files.length === 0 || new Set(files.map((f) => f.disk_id)).size !== 1) return null
  const parts = files.map((f) => f.relative_path.split('/').slice(0, -1))
  const common: string[] = []
  for (let i = 0; i < parts[0].length; i++) {
    if (parts.every((p) => p[i] === parts[0][i])) common.push(parts[0][i])
    else break
  }
  return common.length ? { diskId: files[0].disk_id, path: common.join('/') } : null
}

// Gli step dell'esecuzione dopo l'approvazione (nazgarr/upload/execute.py): torrent
// e screenshot se c'è almeno un upload, poi un tracker alla volta, poi la
// fine. Quelli conclusi restano segnati mentre il worker va avanti.
export type ExecutionStepState = 'pending' | 'active' | 'done' | 'failed' | 'skipped'
export interface ExecutionStep {
  key: string
  label: string
  state: ExecutionStepState
}

interface ExecutionJob {
  status: string
  stage: string | null
  events: { code: string }[]
  targets: { id: number; tracker_label: string; action: string | null; status: string }[]
}

const TARGET_ACTIVE = ['preparing', 'uploading', 'seeding']

export function executionSteps(job: ExecutionJob): ExecutionStep[] {
  const codes = new Set(job.events.map((event) => event.code))
  const stage = job.stage ?? ''
  const onTrackers = stage.startsWith('tracker:') || !['queued', 'running'].includes(job.status)
  const targets = job.targets.filter((target) => target.action && target.action !== 'skip')
  const steps: ExecutionStep[] = []

  if (targets.some((target) => target.action === 'upload')) {
    // Un upload riuscito ha avuto per forza torrent e screenshot (anche nei
    // job di prima di questi eventi).
    const uploaded = targets.some((target) => target.action === 'upload' && target.status === 'done')
    const hashed = uploaded || codes.has('torrent_created')
    const shot = uploaded || codes.has('screenshots_done')
    const hashing: ExecutionStepState = hashed
      ? 'done'
      : stage === 'hashing'
        ? 'active'
        : stage === 'screenshots' || onTrackers
          ? 'failed'
          : 'pending'
    const screenshots: ExecutionStepState = shot
      ? 'done'
      : hashing === 'failed'
        ? 'skipped'
        : stage === 'screenshots'
          ? 'active'
          : onTrackers
            ? 'failed'
            : 'pending'
    steps.push({ key: 'hashing', label: t('upload.steps.hashing'), state: hashing })
    steps.push({ key: 'screenshots', label: t('upload.steps.screenshots'), state: screenshots })
  }

  for (const target of targets) {
    const state: ExecutionStepState =
      target.status === 'done'
        ? 'done'
        : target.status === 'failed'
          ? 'failed'
          : target.status === 'cancelled'
            ? 'skipped'
            : TARGET_ACTIVE.includes(target.status) || stage === `tracker:${target.tracker_label}`
              ? 'active'
              : 'pending'
    steps.push({ key: `target-${target.id}`, label: target.tracker_label, state })
  }

  const finished = ['done', 'partial', 'failed'].includes(job.status)
  const finish: ExecutionStepState = finished
    ? job.status === 'done'
      ? 'done'
      : 'failed'
    : job.status === 'cancelled'
      ? 'skipped'
      : 'pending'
  steps.push({ key: 'finish', label: t('upload.steps.finish'), state: finish })
  return steps
}

// Il nome della sorgente di un job: il percorso, o per un pack di file
// scelti a mano (nazgarr/upload/pack.py) il nome del pack.
export function sourceLabel(job: { relative_path: string; pack_name?: string | null }) {
  return job.pack_name ? t('pack.label', { name: job.pack_name }) : job.relative_path
}

// La sorgente (BluRay, WEB-DL...) non sta in MediaInfo: senza un torrent in
// hardlink o un nome di release da cui leggerla, e senza un valore scritto
// a mano, il nome proposto non ce l'ha.
export function sourceMissing(job: { analysis?: unknown; overrides: Record<string, unknown> }): boolean {
  const detected = ((job.analysis as Record<string, unknown> | null)?.detected ?? {}) as Record<string, unknown>
  return !detected.source && !job.overrides.source
}

// I campi dei dettagli rilevati che si possono togliere dal nome (override
// "cleared", nazgarr/upload/naming.py CLEARABLE_FIELDS): non tipo, risoluzione e
// sorgente, che danno gli id del tracker o fanno scattare l'avviso.
export const CLEARABLE_FIELDS = [
  'edition', 'repack', 'hybrid', 'service', 'hdr', 'video_codec', 'audio', 'audio_languages', 'group', 'year',
] as const

export interface FileNamesPreview {
  name: string
  files: string[]
  count: number
  single_file?: boolean
  folder?: string | null
  main_video?: string | null
}

export interface FileNames {
  available: string[]
  default: string | null
  previews: Record<string, FileNamesPreview>
}

type NamedJob = { analysis?: unknown; overrides: Record<string, unknown> }

export function fileNames(job: NamedJob): FileNames | undefined {
  return (job.analysis as { file_names?: FileNames } | null)?.file_names
}

// La modalità dei nomi dei file in uso: quella scelta se c'è ancora, se no la predefinita.
export function fileNamingMode(job: NamedJob): string | null {
  const names = fileNames(job)
  if (!names || names.available.length === 0) return null
  const chosen = job.overrides.file_naming as string | undefined
  return chosen && names.available.includes(chosen) ? chosen : (names.default ?? names.available[0])
}

// Il MediaInfo come lo riceverà il tracker: "Complete name" è il percorso
// dentro il torrent con i nomi scelti, non quello nelle tue cartelle
// (nazgarr/upload/execute.py refresh_mediainfo, all'esecuzione).
export function plannedMediaInfo(job: NamedJob & { mediainfo_text?: string | null }): string | null {
  const text = job.mediainfo_text ?? null
  const mode = fileNamingMode(job)
  const name = mode ? fileNames(job)?.previews[mode]?.main_video : null
  if (!text || !name) return text
  return text.replace(/^(Complete name\s*:\s*).*$/m, (_line, label: string) => label + name)
}
