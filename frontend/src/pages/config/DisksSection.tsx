import { FolderIcon, HardDriveIcon, InfoIcon, PencilIcon, PlusIcon, XIcon, ZapIcon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import {
  useAddDiskFolder,
  useAvailableMounts,
  useCreateDisk,
  useDisks,
  useRemoveDiskFolder,
  useUpdateDisk,
  useVerifyDisk,
} from '@/api/hooks/disks'
import type { Schemas } from '@/api/client'
import { Button } from '@/components/ui/button'
import { ConfirmButton } from '@/components/ConfirmButton'
import { InfoHint } from '@/components/InfoPopover'
import { SettingsHeader } from '@/components/SettingsHeader'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { t } from '@/lib/i18n'
import { DiskDeleteButton } from '@/pages/config/DeleteWithPreview'
import { DiskBrowserDialog } from '@/pages/config/DiskBrowserDialog'
import { autosaveFeedback } from '@/lib/autosave'

type Disk = Schemas['DiskResponse']

type DiskPreset = { label: string; root: string }

// Senza preset: il pulsante "Aggiungi disco". Con un preset (un mount
// proposto): la stessa finestra, aperta da fuori e già compilata.
function AddDiskDialog({ preset, onClose }: { preset?: DiskPreset | null; onClose?: () => void }) {
  const controlled = preset !== undefined
  const [openState, setOpenState] = useState(false)
  const open = controlled ? preset !== null : openState
  const setOpen = (value: boolean) => (controlled ? !value && onClose?.() : setOpenState(value))
  const [label, setLabel] = useState(preset?.label ?? '')
  // null finché nessuno l'ha toccato: allora root_path propone la prima
  // cartella (il mount unico, caso comune, es. /data) così il campo non parte
  // vuoto, e resta modificabile. Derivato al render, non copiato da un effetto.
  const [rootPathDraft, setRootPath] = useState<string | null>(preset?.root ?? null)
  const { data: mounts } = useAvailableMounts()
  const createDisk = useCreateDisk()
  const rootPath = rootPathDraft ?? (mounts?.scan_root ? (mounts.mounts[0] ?? mounts.scan_root) : '')

  function submit() {
    createDisk.mutate(
      { label, root_path: rootPath },
      {
        onSuccess: () => {
          setOpen(false)
          setLabel('')
          setRootPath('')
        },
        onError: (error) => toast.error(t('disks.createDiskFailed', { message: error.message })),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {!controlled && (
        <DialogTrigger render={<Button data-tour="storage.add"><PlusIcon className="size-4" />{t('disks.addDisk')}</Button>} />
      )}
      <DialogContent data-tour="storage.dialog">
        <DialogHeader>
          <DialogTitle>{t('disks.addDisk')}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5" data-tour="storage.dialog.label">
            <Label htmlFor="disk-label">{t('disks.label')}</Label>
            <Input id="disk-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="main" />
          </div>
          <div className="grid gap-1.5" data-tour="storage.dialog.root">
            <Label htmlFor="disk-root-path">root_path</Label>
            <Input
              id="disk-root-path"
              value={rootPath}
              onChange={(e) => setRootPath(e.target.value)}
              placeholder="/mnt/disk1"
            />
            <p className="text-xs text-muted-foreground">{t('disks.rootPathHelp')}</p>
          </div>
          {mounts?.mounts.length ? (
            <div className="grid gap-1.5">
              <Label>{t('disks.chooseSubfolder')}</Label>
              <Select value={rootPath} onValueChange={(v) => v != null && setRootPath(v)}>
                <SelectTrigger>
                  <SelectValue placeholder={t('disks.subfolderPlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  {mounts.mounts.map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">{t('disks.multiDiskHelp')}</p>
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button data-tour="storage.dialog.create" onClick={submit} disabled={!label || !rootPath || createDisk.isPending}>
            {t('disks.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// La prova del disco (nazgarr/library/disk_folders.py test_disk): cartelle, filesystem e
// un hardlink di prova fra le cartelle, come "Prova connessione" dei client.
// st_dev cambiato da solo è un avviso: su FUSE cambia a ogni rimontaggio.
function DiskTestButton({ diskId }: { diskId: number }) {
  const verify = useVerifyDisk()
  return (
    <Button
      variant="outline"
      size="sm"
      data-tour="storage.verify"
      title={t('disks.testHelp')}
      disabled={verify.isPending}
      onClick={() =>
        verify.mutate(diskId, {
          onSuccess: (result) => {
            const checks = result.checks ?? []
            const links = checks.filter((c) => c.code === 'hardlink_ok').length
            // st_dev cambiato: aggiornato se gli hardlink funzionano, se no lasciato com'era.
            // Inode diversi: il consiglio dipende dal filesystem (hint: smb, mergerfs, fuse, other).
            const key = (c: (typeof checks)[number]) => {
              const params = c.params as { updated?: boolean; hint?: string }
              if (c.code === 'st_dev_changed' && !params.updated) return 'st_dev_changed_kept'
              if (c.code === 'hardlink_inode_mismatch') return `hardlink_inode_mismatch_${params.hint ?? 'other'}`
              return c.code
            }
            const notes = checks
              .filter((c) => c.level !== 'ok')
              .map((c) => t(`disks.check.${key(c)}`, c.params as Record<string, string | number>))
            const description = notes.length ? (
              <ul className="grid gap-1">{notes.map((note) => <li key={note}>{note}</li>)}</ul>
            ) : undefined
            if (!result.consistent) toast.error(t('disks.testFailed'), { description, duration: 15000 })
            else if (notes.length) toast.warning(t('disks.testPassedWithNotes', { count: links }), { description, duration: 10000 })
            else toast.success(t('disks.testPassed', { count: links }))
          },
          onError: (error) => toast.error(t('disks.verificationFailed', { message: error.message })),
        })
      }
    >
      <ZapIcon className="size-4" />
      {t('disks.test')}
    </Button>
  )
}

function RelPathCell({
  diskId,
  value,
  field,
  title,
  emptyLabel,
  tour,
}: {
  tour?: string
  diskId: number
  value: string | null
  field: 'new_torrent_rel_path' | 'upload_rel_path' | 'watch_rel_path'
  title: string
  emptyLabel?: string
}) {
  const [browserOpen, setBrowserOpen] = useState(false)
  const updateDisk = useUpdateDisk()

  return (
    <>
      <span className="inline-flex items-center gap-1">
        <button
          className="font-mono text-xs text-muted-foreground hover:underline"
          data-tour={tour}
          data-tour-filled={value ? 'true' : undefined}
          onClick={() => setBrowserOpen(true)}
        >
          {value || emptyLabel || t('disks.setPath')}
        </button>
        {/* Le cartelle facoltative (con un "vuoto" che vuol dire qualcosa) si possono togliere. */}
        {value && emptyLabel && (
          <button
            className="text-muted-foreground hover:text-foreground"
            title={t('disks.clearPath')}
            aria-label={t('disks.clearPath')}
            onClick={() => updateDisk.mutate({ diskId, body: { [field]: '' } }, autosaveFeedback(title))}
          >
            <XIcon className="size-3" />
          </button>
        )}
      </span>
      <DiskBrowserDialog
        diskId={diskId}
        open={browserOpen}
        onOpenChange={setBrowserOpen}
        title={title}
        onSelect={(path) => updateDisk.mutate({ diskId, body: { [field]: path } }, autosaveFeedback(title))}
      />
    </>
  )
}

// Modifica del disco: solo l'etichetta (root_path non cambia, le cartelle
// si gestiscono sulla scheda).
function EditDiskDialog({ disk }: { disk: Disk }) {
  const [open, setOpen] = useState(false)
  const [label, setLabel] = useState(disk.label)
  const updateDisk = useUpdateDisk()

  function submit() {
    updateDisk.mutate(
      { diskId: disk.id, body: { label } },
      {
        onSuccess: () => setOpen(false),
        onError: (error) => toast.error(t('common.saveFailed', { message: error.message })),
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="ghost" size="icon-sm" title={t('common.edit')}><PencilIcon className="size-4" /></Button>} />
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('disks.editDisk')}</DialogTitle>
          <DialogDescription>{t('disks.rootPathNotEditable')}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="disk-edit-label">{t('disks.label')}</Label>
            <Input id="disk-edit-label" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label>root_path</Label>
            <Input value={disk.root_path} disabled />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={submit} disabled={!label || updateDisk.isPending}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Le cartelle media o di seeding di un disco, più di una (nazgarr/library/disk_folders.py):
// ognuna si toglie con la sua X (sul disco non cambia niente), "Aggiungi"
// apre il selettore. Il backend rifiuta quelle che si sovrappongono o stanno
// su un altro filesystem, con il motivo.
function FolderList({
  disk,
  kind,
  title,
  help,
  empty,
  tour,
}: {
  disk: Disk
  kind: 'media' | 'seeding'
  title: string
  help: string
  empty: string
  tour: string
}) {
  const [browserOpen, setBrowserOpen] = useState(false)
  const add = useAddDiskFolder()
  const remove = useRemoveDiskFolder()
  const folders = (disk.folders ?? []).filter((f) => f.kind === kind)
  return (
    <div className="grid gap-1.5">
      <div className="flex items-center justify-between gap-2">
        {/* Cosa va in queste cartelle: dall'icona, al passaggio o al tocco. */}
        <span className="flex items-center gap-1.5 text-xs font-medium">
          {title}
          <InfoHint content={help} label={t('disks.whatIsThis', { what: title })} />
        </span>
        <Button
          variant="ghost"
          size="xs"
          data-tour={tour}
          data-tour-filled={folders.length > 0 ? 'true' : undefined}
          onClick={() => setBrowserOpen(true)}
        >
          <PlusIcon className="size-3.5" />
          {t('disks.addFolder')}
        </Button>
      </div>
      {folders.length === 0 ? (
        <p className="rounded-md border border-dashed px-2.5 py-1.5 text-xs text-muted-foreground">{empty}</p>
      ) : (
        <ul className="grid gap-1">
          {folders.map((folder) => (
            <li key={`${folder.kind}:${folder.relative_path}`} className="flex min-w-0 items-center gap-2 rounded-md border bg-muted/30 px-2.5 py-1">
              <FolderIcon className="size-3.5 shrink-0 text-primary" />
              <span className="min-w-0 flex-1 truncate font-mono text-xs" title={folder.relative_path}>
                {folder.relative_path}
              </span>
              {folder.id != null && (
                // Un'area da size-7 (prima solo l'icona da 14px) e una
                // conferma: al tocco era facile toglierla per sbaglio.
                <ConfirmButton
                  trigger={
                    <button
                      className="-my-1 -mr-1.5 flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                      title={t('disks.removeFolder')}
                      aria-label={t('disks.removeFolder')}
                      disabled={remove.isPending}
                    >
                      <XIcon className="size-3.5" />
                    </button>
                  }
                  title={t('disks.removeFolderTitle', { path: folder.relative_path })}
                  description={t('disks.removeFolderDescription')}
                  confirmLabel={t('disks.removeFolderConfirm')}
                  pending={remove.isPending}
                  onConfirm={() =>
                    remove.mutate({ diskId: disk.id, folderId: folder.id as number }, autosaveFeedback(title))
                  }
                />
              )}
            </li>
          ))}
        </ul>
      )}
      <DiskBrowserDialog
        diskId={disk.id}
        open={browserOpen}
        onOpenChange={setBrowserOpen}
        title={title}
        onSelect={(path) =>
          add.mutate(
            { diskId: disk.id, kind, path },
            { onError: (error) => toast.error(t('disks.addFolderFailed', { message: error.message })), onSuccess: () => toast.success(t('disks.folderAdded', { path })) },
          )
        }
      />
    </div>
  )
}

const WARNING_STYLE = 'flex gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs'

function AddMountButton({ path, onAdd, tour }: { path: string; onAdd: (preset: DiskPreset) => void; tour?: string }) {
  return (
    <Button size="sm" variant="outline" data-tour={tour}
            onClick={() => onAdd({ label: path.split('/').filter(Boolean).pop() ?? 'disk', root: path })}>
      <PlusIcon className="size-4" />
      {t('disks.mounts.add')}
    </Button>
  )
}

// Le cartelle montate nel container (nazgarr/core/mounts.py): ognuna è un
// disco possibile, e gli avvisi sui montaggi che rompono gli hardlink.
function MountsPanel({ onAdd }: { onAdd: (preset: DiskPreset) => void }) {
  const { data } = useAvailableMounts()
  // Tutto già registrato e nessun avviso: niente da dire.
  if (!data || (data.detected.every((m) => m.registered) && data.warnings.length === 0)) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t('disks.mounts.title')}</CardTitle>
        <p className="text-xs text-muted-foreground">
          {data.scope_source === 'config'
            ? t('disks.mounts.scopeConfig', { root: data.scan_root })
            : t('disks.mounts.scopeMounts')}
        </p>
      </CardHeader>
      <CardContent className="grid gap-2 text-sm">
        {data.detected.map((mount) => (
          <div key={mount.path} className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2">
            <HardDriveIcon className="size-4 shrink-0 text-muted-foreground" />
            <span className="font-mono text-xs">{mount.path}</span>
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {mount.unraid_share ? t('disks.mounts.unraidShare') : mount.fstype}
            </span>
            <span className="flex-1" />
            {mount.registered ? (
              <span className="text-xs text-muted-foreground">{t('disks.mounts.registered')}</span>
            ) : (
              data.mounts.includes(mount.path) &&
              (data.mounts[0] === mount.path ? (
                <AddMountButton tour="storage.mount-add" path={mount.path} onAdd={onAdd} />
              ) : (
                <AddMountButton path={mount.path} onAdd={onAdd} />
              ))
            )}
          </div>
        ))}
        {data.warnings.map((warning) => (
          <div key={warning.code} className={WARNING_STYLE}>
            <InfoIcon className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>
              {t(`disks.mounts.warning.${warning.code}`, {
                paths: warning.paths.join(', '),
                share: warning.share.join(', '),
                disks: warning.disks.join(', '),
                root: warning.scan_root ?? '',
              })}
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export function DisksSection() {
  const { data: disks, isPending } = useDisks()
  const [preset, setPreset] = useState<DiskPreset | null>(null)

  return (
    <div className="grid content-start gap-4">
      <div data-tour="storage.card">
        <SettingsHeader title={t('config.tabStorage')} description={t('config.descStorage')} action={<AddDiskDialog />} />
      </div>
      {isPending && <p className="text-sm text-muted-foreground">{t('common.loading')}</p>}
      <MountsPanel onAdd={setPreset} />
      {preset && <AddDiskDialog key={preset.root} preset={preset} onClose={() => setPreset(null)} />}
      {disks?.length === 0 && (
        <Card>
          <CardContent className="py-6 text-center text-sm text-muted-foreground">{t('disks.noDisksConfigured')}</CardContent>
        </Card>
      )}
      {/* Senza nessuna cartella media il reseed non trova nulla: oggi riconosce
          solo i file della libreria (riconoscere i file in seed è un passo futuro). */}
      {disks && disks.length > 0 && disks.every((disk) => disk.media_folders.length === 0) && (
        <div className="flex gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
          <InfoIcon className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="grid gap-1">
            <p className="font-medium">{t('disks.noLibraryTitle')}</p>
            <p className="text-muted-foreground">{t('disks.noLibrary')}</p>
          </div>
        </div>
      )}
      {/* Una scheda per disco, come client e tracker: le sue cartelle media e
          di seeding (più di una), poi quelle per i nuovi hardlink, gli upload
          e le release. */}
      <div className="grid gap-4 xl:grid-cols-2 2xl:grid-cols-3">
        {disks?.map((disk) => (
          <Card key={disk.id} data-tour="storage.row" className="min-w-0">
            <CardHeader className="flex flex-row items-center gap-3">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted">
                <HardDriveIcon className="size-4.5 text-muted-foreground" />
              </span>
              <div className="grid min-w-0 flex-1 gap-0.5">
                <CardTitle className="truncate text-base">{disk.label}</CardTitle>
                <span className="truncate font-mono text-xs text-muted-foreground" title={disk.root_path}>
                  {disk.root_path}
                </span>
              </div>
            </CardHeader>
            <CardContent className="grid min-w-0 gap-4 text-sm">
              <FolderList
                disk={disk}
                kind="seeding"
                title={t('disks.seedingFolders')}
                help={t('disks.seedingFoldersHelp')}
                empty={t('disks.noSeedingFolder')}
                tour="storage.seeding-folder"
              />
              <FolderList
                disk={disk}
                kind="media"
                title={t('disks.mediaFolders')}
                help={t('disks.mediaFoldersHelp')}
                empty={t('disks.noMediaFolder')}
                tour="storage.media-folder"
              />
              <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1.5 text-xs">
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  {t('disks.newHardlinkFolderColumn')}
                  <InfoHint content={t('disks.newHardlinkFolderHelp')}
                            label={t('disks.whatIsThis', { what: t('disks.newHardlinkFolderColumn') })} />
                </span>
                {/* Dove l'executor crea i NUOVI hardlink: vuoto = la prima cartella di seeding. */}
                <RelPathCell
                  diskId={disk.id}
                  value={disk.new_torrent_rel_path}
                  field="new_torrent_rel_path"
                  title={t('disks.newHardlinkFolderLabel')}
                  tour="storage.new-folder"
                  emptyLabel={t('disks.sameAsSeedingFolder')}
                />
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  {t('disks.uploadFolderColumn')}
                  <InfoHint content={t('disks.uploadFolderHelp')} label={t('disks.whatIsThis', { what: t('disks.uploadFolderColumn') })} />
                </span>
                {/* Dove il flusso di upload crea gli hardlink: vuoto = la prima cartella di seeding. */}
                <RelPathCell
                  diskId={disk.id}
                  value={disk.upload_rel_path}
                  field="upload_rel_path"
                  title={t('disks.uploadFolderLabel')}
                  tour="storage.upload-folder"
                  emptyLabel={t('disks.sameAsSeedingFolder')}
                />
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  {t('disks.watchFolderColumn')}
                  <InfoHint content={t('disks.watchFolderHelp')} label={t('disks.whatIsThis', { what: t('disks.watchFolderColumn') })} />
                </span>
                {/* Le release nuove qui dentro partono da sole fino alla decisione (nazgarr/upload/watch.py). */}
                <RelPathCell
                  diskId={disk.id}
                  value={disk.watch_rel_path ?? null}
                  field="watch_rel_path"
                  title={t('disks.watchFolderLabel')}
                  tour="storage.watch-folder"
                  emptyLabel={t('disks.notWatched')}
                />
              </div>
              <div className="flex items-center gap-1 border-t pt-3">
                <DiskTestButton diskId={disk.id} />
                <span className="flex-1" />
                <EditDiskDialog disk={disk} />
                <DiskDeleteButton disk={disk} />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
