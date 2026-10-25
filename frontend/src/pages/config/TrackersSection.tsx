import { CheckIcon, FileUpIcon, PencilIcon, PlusIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { useBundledUploadProfiles, useCreateTracker, useTrackers, useUpdateTracker } from '@/api/hooks/trackers'
import type { Schemas } from '@/api/client'
import { SettingsHeader } from '@/components/SettingsHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { InfoPopover } from '@/components/InfoPopover'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useTorrentClients } from '@/api/hooks/torrentClients'
import { usePlugins } from '@/api/hooks/plugins'
import { AdapterConfigFields } from '@/components/AdapterConfigFields'
import { configPayload, initialConfigValues, missingRequired, type ConfigValues } from '@/lib/adapterConfig'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { t, uiLocale } from '@/lib/i18n'
import { TrackerDeleteButton } from '@/pages/config/DeleteWithPreview'
import { InsecureUrlWarning } from '@/pages/config/InsecureUrlWarning'
import { insecureUrlWarning } from '@/pages/config/insecureUrl'
import { TrackerLogo } from '@/pages/config/ServiceIcons'
import { safeHref } from '@/lib/safeUrl'
import { cn, selectLabel } from '@/lib/utils'
import { TrackerSeedRequirement } from '@/pages/config/TrackerSeedRequirement'
import { UploadProfileDialog } from '@/pages/config/UploadProfileDialog'
import { autosaveFeedback } from '@/lib/autosave'

type Tracker = Schemas['TrackerResponse']

// Facoltativa e quasi sempre inutile: la chiave dei link di download viene
// appresa dalle risposte dell'API del tracker (nazgarr/adapters/tracker/base.py).
// Serve solo a riscrivere i link salvati da Sonarr/Radarr prima di un
// cambio di chiave, e mai restituita dall'API (has_rss_key).
function RssKeyField({
  id,
  value,
  onChange,
  placeholder,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  placeholder: string
}) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{t('trackers.rssKey')}</Label>
      <Input
        id={id}
        type="password"
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      <p className="text-xs text-muted-foreground">{t('trackers.rssKeyHelp')}</p>
    </div>
  )
}

const FIRST_ENABLED = 'first'
const NO_LANGUAGE = 'none'
// Le lingue che il naming sa scrivere (nazgarr/upload/naming.py LANG3), ISO 639-1.
const LANGUAGES = [
  'ar', 'cs', 'da', 'de', 'el', 'en', 'es', 'fi', 'fr', 'he', 'hi', 'hu', 'it', 'ja', 'ko', 'nl', 'no', 'pl', 'pt',
  'ro', 'ru', 'sv', 'th', 'tr', 'uk', 'zh',
]

function languageName(code: string) {
  try {
    return new Intl.DisplayNames([uiLocale()], { type: 'language' }).of(code) ?? code
  } catch {
    return code
  }
}

// La lingua del tracker: per i nomi degli upload (titolo localizzato, lingua
// messa per prima, SUB/SUBS con la lingua).
function TrackerLanguageSelect({ value, onChange }: { value: string | null; onChange: (code: string | null) => void }) {
  const options = [
    { value: NO_LANGUAGE, label: t('trackers.noLanguage') },
    ...LANGUAGES.map((code) => ({ value: code, label: `${languageName(code)} (${code})` })).sort((a, b) =>
      a.label.localeCompare(b.label),
    ),
  ]
  const current = value ?? NO_LANGUAGE
  return (
    <Select value={current} onValueChange={(v) => onChange(v === NO_LANGUAGE || v == null ? null : v)}>
      <SelectTrigger size="sm" className="w-full max-w-44">
        <SelectValue>
          {(v: string | null) => selectLabel(options, v, (o) => o.value, (o) => o.label, t('trackers.noLanguage'))}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// In quale client aggiungere i torrent di questo tracker quando si ricrea
// un seed (es. l'istanza dei tracker privati). "First enabled" = il primo
// client abilitato, il comportamento di sempre.
function TrackerClientSelect({ value, onChange }: { value: number | null; onChange: (id: number | null) => void }) {
  const { data: clients } = useTorrentClients()
  const options = [
    { value: FIRST_ENABLED, label: t('trackers.firstEnabledClient') },
    ...(clients ?? []).map((c) => ({ value: String(c.id), label: c.label })),
  ]
  const current = value == null ? FIRST_ENABLED : String(value)
  return (
    <Select value={current} onValueChange={(v) => onChange(v === FIRST_ENABLED || v == null ? null : Number(v))}>
      <SelectTrigger size="sm" className="w-full max-w-44">
        <SelectValue>
          {(v: string | null) => selectLabel(options, v, (o) => o.value, (o) => o.label, t('trackers.firstEnabledClient'))}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// I tracker dei plugin, con i campi che dichiarano.
function usePluginTrackerTypes() {
  const { data } = usePlugins()
  return (data?.adapters ?? []).filter((a) => a.kind === 'tracker' && a.plugin)
}

function AddTrackerDialog() {
  const [open, setOpen] = useState(false)
  const [adapterType, setAdapterType] = useState('unit3d')
  const pluginTypes = usePluginTrackerTypes()
  const pluginSpec = pluginTypes.find((a) => a.adapter_type === adapterType)
  const [config, setConfig] = useState<ConfigValues>({})
  // Torznab: un endpoint (Prowlarr, Jackett...) solo per cercare e
  // reseedare, senza preset, chiave RSS né profilo di upload. L'announce è
  // facoltativo: riconosce nei client i torrent dell'indexer.
  const isTorznab = adapterType === 'torznab'
  const typeOptions = [
    { value: 'unit3d', label: 'UNIT3D' },
    { value: 'torznab', label: t('trackers.typeTorznab') },
    ...pluginTypes.map((a) => ({ value: a.adapter_type, label: `${a.label} · ${a.plugin}` })),
  ]
  const [presetKey, setPresetKey] = useState('')
  const [label, setLabel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiToken, setApiToken] = useState('')
  const [announceUrl, setAnnounceUrl] = useState('')
  const [rssKey, setRssKey] = useState('')
  const createTracker = useCreateTracker()
  const { data: bundled } = useBundledUploadProfiles()
  // Dove il tracker mostra il proprio announce URL: dal preset scelto, o da
  // quello dello stesso indirizzo (come fa il server per il profilo).
  const hostOf = (url: string | null | undefined) => {
    try {
      return new URL(url ?? '').hostname.replace(/^www\./, '')
    } catch {
      return ''
    }
  }
  const matched = bundled?.find((p) => p.key === presetKey)
    ?? bundled?.find((p) => p.base_url && hostOf(p.base_url) === hostOf(baseUrl))
  const announcePage = matched?.announce_url_page
  // Da un profilo incluso, o da un'altra istanza attraverso il proxy: un link
  // solo se è http(s), altrimenti l'indirizzo come testo.
  const announceHref = safeHref(announcePage)

  function applyPreset(key: string) {
    setPresetKey(key)
    const preset = bundled?.find((p) => p.key === key)
    if (!preset) return
    if (!label) setLabel(preset.label)
    if (preset.base_url) setBaseUrl(preset.base_url)
  }

  function reset() {
    setPresetKey('')
    setLabel('')
    setBaseUrl('')
    setApiToken('')
    setAnnounceUrl('')
    setRssKey('')
    setConfig({})
  }

  function submit() {
    createTracker.mutate(
      {
        label,
        adapter_type: adapterType,
        base_url: baseUrl,
        api_token: apiToken,
        announce_url: announceUrl || undefined,
        rss_key: (!isTorznab && rssKey) || undefined,
        // Il preset scelto porta il suo profilo di upload; senza, il server
        // usa quello dell'indirizzo se lo conosce (es. ITT).
        upload_profile: isTorznab ? 'none' : presetKey || 'auto',
        ...(pluginSpec ? { config: configPayload(pluginSpec.config_fields, config) } : {}),
      },
      {
        onSuccess: () => {
          setOpen(false)
          reset()
        },
        onError: (error) => toast.error(t('trackers.createFailed', { message: error.message })),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button data-tour="trackers.add"><PlusIcon className="size-4" />{t('trackers.addTracker')}</Button>} />
      <DialogContent data-tour="trackers.dialog">
        <DialogHeader>
          <DialogTitle>{t('trackers.addTracker')}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3">
          {!isTorznab && (
          <div className="grid gap-1.5" data-tour="trackers.dialog.preset">
            <Label>{t('trackers.preset')}</Label>
            <Select value={presetKey} onValueChange={(v) => v != null && applyPreset(v)}>
              <SelectTrigger>
                <SelectValue placeholder={t('trackers.presetPlaceholder')}>
                  {(v: string | null) =>
                    selectLabel(bundled, v, (p) => p.key, (p) => p.label, t('trackers.presetPlaceholder'))
                  }
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {bundled?.map((p) => (
                  <SelectItem key={p.key} value={p.key}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('trackers.presetHelp')}</p>
          </div>
          )}
          <div className="grid gap-1.5">
            <Label htmlFor="t-label">{t('trackers.label')}</Label>
            <Input id="t-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="mytracker" />
          </div>
          <div className="grid gap-1.5">
            <Label>{t('trackers.type')}</Label>
            <Select
              value={adapterType}
              onValueChange={(v) => {
                if (v == null) return
                setAdapterType(v)
                const spec = pluginTypes.find((a) => a.adapter_type === v)
                setConfig(spec ? initialConfigValues(spec.config_fields, undefined) : {})
              }}
            >
              <SelectTrigger>
                <SelectValue>{(v: string | null) => selectLabel(typeOptions, v, (o) => o.value, (o) => o.label, 'UNIT3D')}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {typeOptions.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5" data-tour="trackers.dialog.url">
            <Label htmlFor="t-base-url">{isTorznab ? t('trackers.torznabUrl') : t('trackers.apiUrl')}</Label>
            <Input
              id="t-base-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={isTorznab ? 'http://prowlarr:9696/1/api' : 'https://mytracker.example'}
            />
            {isTorznab && <p className="text-xs text-muted-foreground">{t('trackers.torznabUrlHelp')}</p>}
            {insecureUrlWarning(baseUrl) && <InsecureUrlWarning kind="tracker" />}
          </div>
          <div className="grid gap-1.5" data-tour="trackers.dialog.token">
            <Label htmlFor="t-api-token">{isTorznab ? t('trackers.torznabApiKey') : t('trackers.apiToken')}</Label>
            <Input id="t-api-token" value={apiToken} onChange={(e) => setApiToken(e.target.value)} />
          </div>
          <div className="grid gap-1.5" data-tour="trackers.dialog.announce">
            <Label htmlFor="t-announce-url">{isTorznab ? t('trackers.torznabAnnounceUrl') : t('trackers.announceUrl')}</Label>
            <Input
              id="t-announce-url"
              value={announceUrl}
              onChange={(e) => setAnnounceUrl(e.target.value)}
              placeholder="https://mytracker.example/announce/passkey"
            />
            {isTorznab && <p className="text-xs text-muted-foreground">{t('trackers.torznabAnnounceUrlHelp')}</p>}
            {!isTorznab && announcePage && (
              <p className="text-xs text-muted-foreground">
                {t('trackers.announceUrlWhere')}{' '}
                {announceHref
                  ? <a href={announceHref} target="_blank" rel="noreferrer" className="underline">{announcePage}</a>
                  : <span className="break-all">{announcePage}</span>}
              </p>
            )}
          </div>
          {!isTorznab && (
            <RssKeyField id="t-rss-key" value={rssKey} onChange={setRssKey} placeholder={t('trackers.rssKeyPlaceholder')} />
          )}
          {pluginSpec && (
            <AdapterConfigFields idPrefix="t-config" fields={pluginSpec.config_fields} values={config} onChange={setConfig} />
          )}
        </div>
        <DialogFooter>
          <Button
            data-tour="trackers.dialog.create"
            onClick={submit}
            disabled={
              !label ||
              !baseUrl ||
              // Un tracker di un plugin può non usare il token API di UNIT3D.
              (!pluginSpec && !apiToken) ||
              (pluginSpec != null && missingRequired(pluginSpec.config_fields, config)) ||
              createTracker.isPending
            }
          >
            {t('trackers.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function EditTrackerDialog({ tracker }: { tracker: Tracker }) {
  const [open, setOpen] = useState(false)
  const [label, setLabel] = useState(tracker.label)
  const [baseUrl, setBaseUrl] = useState(tracker.base_url)
  const [apiToken, setApiToken] = useState('')
  const [announceUrl, setAnnounceUrl] = useState('')
  const [rateLimit, setRateLimit] = useState(tracker.rate_limit_per_min?.toString() ?? '')
  const [rssKey, setRssKey] = useState('')
  const updateTracker = useUpdateTracker()
  const pluginSpec = usePluginTrackerTypes().find((a) => a.adapter_type === tracker.adapter_type)
  const secretsSet = (tracker.config as { secrets_set?: string[] }).secrets_set ?? []
  const [config, setConfig] = useState<ConfigValues | null>(null)
  const configValues =
    config ??
    (pluginSpec ? initialConfigValues(pluginSpec.config_fields, (tracker.config as { values?: Record<string, unknown> }).values) : {})

  function submit() {
    updateTracker.mutate(
      {
        id: tracker.id,
        body: {
          label,
          base_url: baseUrl,
          api_token: apiToken || undefined,
          announce_url: announceUrl || undefined,
          rate_limit_per_min: rateLimit ? Number(rateLimit) : undefined,
          rss_key: rssKey || undefined,
          ...(pluginSpec ? { config: configPayload(pluginSpec.config_fields, configValues) } : {}),
        },
      },
      {
        onSuccess: () => {
          setOpen(false)
          setApiToken('')
          setAnnounceUrl('')
          setRssKey('')
          setConfig(null)
        },
        onError: (error) => toast.error(t('common.saveFailed', { message: error.message })),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="ghost" size="icon-sm" title={t('common.edit')}><PencilIcon className="size-4" /></Button>} />
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('trackers.editTracker')}</DialogTitle>
          <DialogDescription>{t('trackers.editTypeLocked')}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="t-edit-label">{t('trackers.label')}</Label>
            <Input id="t-edit-label" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="t-edit-base-url">{t('trackers.apiUrl')}</Label>
            <Input id="t-edit-base-url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
            {insecureUrlWarning(baseUrl, tracker) && <InsecureUrlWarning kind="tracker" />}
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="t-edit-api-token">{t('trackers.apiToken')}</Label>
            <Input
              id="t-edit-api-token"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder={t('common.leaveBlank')}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="t-edit-announce-url">{t('trackers.announceUrl')}</Label>
            <Input
              id="t-edit-announce-url"
              value={announceUrl}
              onChange={(e) => setAnnounceUrl(e.target.value)}
              placeholder={tracker.has_announce_url ? t('trackers.announceUrlKnown') : undefined}
            />
          </div>
          <RssKeyField
            id="t-edit-rss-key"
            value={rssKey}
            onChange={setRssKey}
            placeholder={tracker.has_rss_key ? t('trackers.rssKeyKnown') : t('trackers.rssKeyPlaceholder')}
          />
          <div className="grid gap-1.5">
            <Label htmlFor="t-edit-rate-limit">{t('trackers.rateLimit')}</Label>
            <Input
              id="t-edit-rate-limit"
              type="number"
              min={0}
              max={600}
              value={rateLimit}
              onChange={(e) => setRateLimit(e.target.value)}
            />
          </div>
          {pluginSpec && (
            <AdapterConfigFields
              idPrefix={`t-edit-config-${tracker.id}`}
              fields={pluginSpec.config_fields}
              values={configValues}
              secretsSet={secretsSet}
              onChange={setConfig}
            />
          )}
        </div>
        <DialogFooter>
          <Button
            onClick={submit}
            disabled={
              !label ||
              !baseUrl ||
              updateTracker.isPending ||
              (pluginSpec != null && missingRequired(pluginSpec.config_fields, configValues, secretsSet))
            }
          >
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Announce URL e chiave RSS contengono la passkey: la tabella dice solo se
// ci sono, l'API non le restituisce mai (has_announce_url, has_rss_key).
function SecretPresence({ present }: { present: boolean }) {
  return present ? (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <CheckIcon className="size-3.5 text-emerald-500" />
      {t('trackers.secretSet')}
    </span>
  ) : (
    <span className="text-xs text-muted-foreground">—</span>
  )
}

export function TrackersSection() {
  const { data: trackers, isPending } = useTrackers()
  const updateTracker = useUpdateTracker()
  const [profileTrackerId, setProfileTrackerId] = useState<number | null>(null)

  return (
    <div className="grid content-start gap-4">
      <SettingsHeader title={t('trackers.title')} description={t('trackers.sectionDescription')} action={<AddTrackerDialog />} />
      {isPending && <p className="text-sm text-muted-foreground">{t('common.loading')}</p>}
      {trackers?.length === 0 && (
        <Card>
          <CardContent className="py-6 text-center text-sm text-muted-foreground">{t('trackers.noTrackers')}</CardContent>
        </Card>
      )}
      {/* Una scheda per tracker: icona del sito, indirizzi e chiavi, client
          su cui seedano i suoi torrent, profilo di upload e azioni. */}
      <div className="grid gap-4 xl:grid-cols-2 2xl:grid-cols-3">
        {trackers?.map((tracker) => {
          const profile = tracker.upload_profile
          return (
            <Card key={tracker.id} data-tour="trackers.card" className={cn('min-w-0', !tracker.enabled && 'opacity-70')}>
              <CardHeader className="flex flex-row items-center gap-3">
                <TrackerLogo trackerId={tracker.id} />
                <div className="grid min-w-0 flex-1 gap-0.5">
                  <CardTitle className="truncate text-base">{tracker.label}</CardTitle>
                  <span className="truncate font-mono text-xs text-muted-foreground" title={tracker.base_url}>
                    {tracker.base_url}
                  </span>
                  {tracker.insecure_public_url && <InsecureUrlWarning kind="tracker" />}
                </div>
                <Switch
                  checked={tracker.enabled}
                  title={t('trackers.enabled')}
                  onCheckedChange={(enabled) =>
                    updateTracker.mutate(
                      { id: tracker.id, body: { enabled } },
                      autosaveFeedback(`${tracker.label} · ${t('trackers.enabled')}`),
                    )
                  }
                />
              </CardHeader>
              <CardContent className="grid min-w-0 gap-3 text-sm">
                <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 text-xs">
                  <span className="text-muted-foreground">{t('trackers.announceUrlColumn')}</span>
                  <SecretPresence present={tracker.has_announce_url} />
                  <span className="text-muted-foreground">{t('trackers.rssKeyColumn')}</span>
                  <SecretPresence present={tracker.has_rss_key} />
                  <span className="text-muted-foreground" data-tour="trackers.card.client">{t('trackers.clientColumn')}</span>
                  <TrackerClientSelect
                    value={tracker.torrent_client_id ?? null}
                    onChange={(torrentClientId) =>
                      updateTracker.mutate(
                        { id: tracker.id, body: { torrent_client_id: torrentClientId } },
                        autosaveFeedback(`${tracker.label} · ${t('trackers.clientColumn')}`),
                      )
                    }
                  />
                  <span className="text-muted-foreground" data-tour="trackers.card.language">
                    <InfoPopover content={t('trackers.languageHelp')} className="pointer-coarse:underline pointer-coarse:decoration-dotted pointer-coarse:underline-offset-2">{t('trackers.languageColumn')}</InfoPopover>
                  </span>
                  <TrackerLanguageSelect
                    value={tracker.language ?? null}
                    onChange={(language) =>
                      updateTracker.mutate(
                        { id: tracker.id, body: { language } },
                        autosaveFeedback(`${tracker.label} · ${t('trackers.languageColumn')}`),
                      )
                    }
                  />
                  <span className="text-muted-foreground" data-tour="trackers.card.seed">
                    <InfoPopover content={t('trackers.seedRequirement.help')} className="pointer-coarse:underline pointer-coarse:decoration-dotted pointer-coarse:underline-offset-2">{t('trackers.seedRequirement.column')}</InfoPopover>
                  </span>
                  <TrackerSeedRequirement
                    tracker={tracker}
                    onChange={(patch) =>
                      updateTracker.mutate(
                        { id: tracker.id, body: patch },
                        autosaveFeedback(`${tracker.label} · ${t('trackers.seedRequirement.column')}`),
                      )
                    }
                  />
                  <span className="text-muted-foreground">{t('trackers.uploadProfile')}</span>
                  <span className="flex flex-wrap items-center gap-1.5">
                    {profile ? (
                      <>
                        <Badge variant="secondary">{profile.source_profile_key ?? t('trackers.customProfile')}</Badge>
                        {profile.naming_version != null && (
                          <span className="text-muted-foreground">
                            {t('trackers.namingVersion', { version: profile.naming_version })}
                          </span>
                        )}
                        {profile.naming_update_available != null && (
                          <Badge variant="outline" className="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300">
                            {t('trackers.namingUpdateBadge')}
                          </Badge>
                        )}
                        {profile.freeleech_options.length > 0 && (
                          <span className="text-muted-foreground">FL {profile.freeleech_options.join('/')}%</span>
                        )}
                      </>
                    ) : (
                      <span className="text-muted-foreground">{t('trackers.noUploadProfile')}</span>
                    )}
                  </span>
                </div>
                <div className="flex items-center gap-1 border-t pt-3">
                  <Button variant="outline" size="sm" data-tour="trackers.card.profile" onClick={() => setProfileTrackerId(tracker.id)}>
                    <FileUpIcon className="size-4" />
                    {t('trackers.uploadProfile')}
                  </Button>
                  <span className="flex-1" />
                  <EditTrackerDialog tracker={tracker} />
                  <TrackerDeleteButton tracker={tracker} />
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {profileTrackerId !== null && (
        <UploadProfileDialog
          trackerId={profileTrackerId}
          trackerLabel={trackers?.find((t) => t.id === profileTrackerId)?.label ?? ''}
          trackerBaseUrl={trackers?.find((t) => t.id === profileTrackerId)?.base_url ?? ''}
          open
          onOpenChange={(open) => !open && setProfileTrackerId(null)}
        />
      )}
    </div>
  )
}
