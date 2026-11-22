import { fireEvent, render, screen } from '@testing-library/react'
import { isValidElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DisksSection } from '@/pages/config/DisksSection'

const remove = vi.fn()
const disk = {
  id: 1, label: 'main', root_path: '/data', st_dev: 1, new_torrent_rel_path: null, upload_rel_path: null,
  watch_rel_path: null, media_rel_path: 'movies', torrents_rel_path: 'torrents',
  media_folders: ['movies', 'tv'], seeding_folders: ['torrents'],
  folders: [
    { id: 10, kind: 'seeding', relative_path: 'torrents' },
    { id: 11, kind: 'media', relative_path: 'movies' },
    { id: 12, kind: 'media', relative_path: 'tv' },
  ],
}
const mutation = { mutate: vi.fn(), isPending: false }
const deleteDisk = vi.fn()
const preview = {
  current: {
    folders: 3, media_files: 1200, seed_files: 800, reviews: 4, executions: 2, match_attempts: 0, watch_entries: 0,
    client_links: 1, upload_jobs: 5, busy: null as string | null,
  },
}
const refetch = vi.fn()
const verifyDisk = vi.fn()
const toast = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn(), success: vi.fn() }))
vi.mock('sonner', () => ({ toast }))
const previewQuery = { isFetching: false, error: null as Error | null, failed: false }

vi.mock('@/api/hooks/disks', () => ({
  useDisks: () => ({ data: [disk], isPending: false }),
  useAvailableMounts: () => ({ data: { scan_root: '/data', scan_roots: ['/data'], scope_source: 'mounts', mounts: [], detected: [], warnings: [] } }),
  useCreateDisk: () => mutation,
  useUpdateDisk: () => mutation,
  useDeleteDisk: () => ({ mutate: deleteDisk, isPending: false }),
  useDiskDeletePreview: (_id: number, enabled: boolean) => ({
    data: enabled && !previewQuery.failed ? preview.current : undefined,
    isFetching: enabled && previewQuery.isFetching,
    error: enabled ? previewQuery.error : null,
    refetch,
  }),
  useVerifyDisk: () => ({ mutate: verifyDisk, isPending: false, data: undefined }),
  useAddDiskFolder: () => mutation,
  useRemoveDiskFolder: () => ({ mutate: remove, isPending: false }),
  useBrowseDisk: () => ({ data: undefined, isPending: false }),
  useMkdir: () => mutation,
}))


describe('DisksSection', () => {
  it('shows every media and seeding folder of a disk, each removable', () => {
    render(<DisksSection />)

    expect(screen.getByText('main')).toBeTruthy()
    for (const folder of ['torrents', 'movies', 'tv']) expect(screen.getByText(folder)).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /Add/ }).length).toBeGreaterThanOrEqual(2)

    // Togliere una cartella ora chiede conferma.
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove the folder (nothing changes on disk)' })[2])
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(remove).toHaveBeenCalledWith({ diskId: 1, folderId: 12 }, expect.anything())
  })

  it('proposes the mounted folder as root_path, and keeps what the user types (even empty)', () => {
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: 'Add disk' }))
    const root = screen.getByLabelText('root_path') as HTMLInputElement
    expect(root.value).toBe('/data')
    fireEvent.change(root, { target: { value: '/data/disk2' } })
    expect(root.value).toBe('/data/disk2')
    fireEvent.change(root, { target: { value: '' } })
    expect(root.value).toBe('')
  })
})

describe('deleting a disk', () => {
  beforeEach(() => {
    previewQuery.isFetching = false
    previewQuery.error = null
    previewQuery.failed = false
    preview.current = { ...preview.current, busy: null }
  })

  it('says what goes with it, that no file is touched, and asks first', () => {
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByText('Library file records')).toBeTruthy()
    expect(screen.getByText((1200).toLocaleString('en-US'))).toBeTruthy()
    expect(screen.queryByText('Search records')).toBeNull() // zero: non si mostra
    expect(screen.getByText('5 uploads from this disk stay in the history, without the disk.')).toBeTruthy()
    expect(screen.getByText('No file on the disk is touched, and torrents stay in their clients.')).toBeTruthy()
    expect(deleteDisk).not.toHaveBeenCalled()

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1)!)
    expect(deleteDisk).toHaveBeenCalledWith(1)
  })

  it('does not offer the delete while a reseed works on its files', () => {
    preview.current = { ...preview.current, busy: 'reseed' }
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByRole('alert').textContent).toContain('A reseed is working on the files of this disk')
    expect((screen.getAllByRole('button', { name: 'Delete' }).at(-1) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not delete without the counts when reading them failed, and offers to read them again', () => {
    previewQuery.failed = true
    previewQuery.error = new Error('boom')
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByText('Could not count what goes with it: boom')).toBeTruthy()
    // Che sul disco non cambia niente si dice comunque.
    expect(screen.getByText('No file on the disk is touched, and torrents stay in their clients.')).toBeTruthy()
    expect((screen.getAllByRole('button', { name: 'Delete' }).at(-1) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(refetch).toHaveBeenCalled()
  })

  it('keeps Delete off while the counts are being read again', () => {
    previewQuery.isFetching = true
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect((screen.getAllByRole('button', { name: 'Delete' }).at(-1) as HTMLButtonElement).disabled).toBe(true)
  })

  it('says why a link with different inodes is not a hardlink, from the filesystem', () => {
    verifyDisk.mockImplementation((_id: number, options: { onSuccess: (result: unknown) => void }) =>
      options.onSuccess({
        consistent: false,
        checks: [{
          code: 'hardlink_inode_mismatch', level: 'error',
          params: { source: 'DownloadArr', folder: 'DownloadArr', fstype: 'cifs', hint: 'smb' },
        }],
      }),
    )
    render(<DisksSection />)

    fireEvent.click(screen.getByRole('button', { name: /Test disk/ }))

    expect(toast.error).toHaveBeenCalledWith('The disk has problems.', expect.anything())
    const { description } = toast.error.mock.calls[0][1] as { description: unknown }
    expect(isValidElement(description)).toBe(true)
    render(<>{description as React.ReactNode}</>)
    expect(screen.getByText(/SMB share without serverino/)).toBeTruthy()
    expect(screen.getByText(/\(cifs\)/)).toBeTruthy()
  })
})
