import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { UploadJob } from '@/api/hooks/uploads'
import { DecisionStep } from '@/components/upload/DecisionStep'

const approve = vi.fn()
const updateOverrides = vi.fn()
vi.mock('@/api/hooks/uploads', () => ({
  useApproveUpload: () => ({ mutate: approve, isPending: false }),
  useUpdateOverrides: () => ({ mutate: updateOverrides, isPending: false }),
  useVerifyTarget: () => ({ mutate: vi.fn(), isPending: false }),
}))

vi.mock('@/components/upload/MatchSummaryCard', () => ({ MatchSummaryCard: () => null }))

const target = (id: number, label: string, extra = {}) => ({
  id, tracker_label: label, status: 'awaiting_decision', suggested_action: 'upload', error_message: null,
  dupes: [], proposed_name: `Movie (2024) 1080p-GRP`, category_id: 1, type_id: 4, resolution_id: 3,
  flags: { anonymous: false, personal_release: false, internal: false, stream: false, freeleech: 0 },
  freeleech_options: [], reseed_torrent_id: null,
  category_id_map: { movie: 1 }, type_id_map: { WEBDL: 4 }, resolution_id_map: { '1080p': 3 }, ...extra,
})

const job = {
  id: 5, status: 'awaiting_decision', year: 2024, overrides: {},
  analysis: { total_size_bytes: 1, file_count: 1, client_matches: [], arr_grabs: [], detected: { group: 'GRP' } },
  targets: [
    target(1, 'ITT'),
    target(2, 'Other', { suggested_action: 'skip', dupes: [
      { torrent_id_remote: '7', name: 'Movie.2024.1080p-X', size_bytes: 1, verdict: 'same_slot', score: 100, reasons: [],
        unknown: [], seeders: 2, size_delta: null, verification: null },
    ] }),
  ],
} as unknown as UploadJob

describe('DecisionStep', () => {
  it('starts from the suggestions and sends every decision after the confirmation', () => {
    render(<DecisionStep job={{ ...job, overrides: { source: 'WEB-DL' } } as UploadJob} />)

    expect(screen.getByText('Every tracker has a decision.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and queue' }))

    expect(approve.mock.calls[0][0].scheduled_at).toBeNull()
    expect(approve.mock.calls[0][0].targets).toEqual([
      { target_id: 1, action: 'upload', name: 'Movie (2024) 1080p-GRP', category_id: 1, type_id: 4, resolution_id: 3,
        flags: { anonymous: false, personal_release: false, internal: false, stream: false, freeleech: 0 },
        reseed_torrent_id: null, client_category: null, client_tags: '' },
      { target_id: 2, action: 'skip', name: null, flags: null, category_id: null, type_id: null, resolution_id: null,
        reseed_torrent_id: null },
    ])
  })

  it('asks for confirmation when a dead torrent is reseeded without claiming its resurrection', () => {
    const passed = { status: 'passed', reason: null, ok: 9, pieces: 9 }
    const dead = {
      torrent_id_remote: '8', name: 'Movie.2024.1080p-Y', size_bytes: 1, verdict: 'identical', score: null,
      reasons: [], unknown: [], seeders: 0, size_delta: null, verification: passed,
    }
    const reseed = {
      ...job,
      overrides: { source: 'WEB-DL' },
      targets: [target(1, 'ITT', { suggested_action: 'reseed', reseed_torrent_id: '8', dupes: [dead],
                                   tracker_base_url: 'https://itt.example' })],
    } as unknown as UploadJob
    render(<DecisionStep job={reseed} />)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(screen.getByText(/ITT: the reseed revives a torrent with no seeders/)).toBeTruthy()
    // Un avviso, mai un blocco: si conferma comunque.
    expect(screen.getByRole('button', { name: 'Queue anyway' })).toBeTruthy()

    // Segnata come rivendicata (qui nella conferma), l'avviso sparisce.
    fireEvent.click(screen.getAllByRole('button', { name: 'Claimed' }).at(-1)!)
    expect(screen.queryByText(/the reseed revives a torrent with no seeders/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and queue' }))
    expect(approve.mock.calls.at(-1)![0].targets[0]).toMatchObject({ action: 'reseed', reseed_torrent_id: '8' })
  })

  it('does not ask about the resurrection when Nazgarr claims it with the website session', () => {
    const passed = { status: 'passed', reason: null, ok: 9, pieces: 9 }
    const dead = {
      torrent_id_remote: '8', name: 'Movie.2024.1080p-Y', size_bytes: 1, verdict: 'identical', score: null,
      reasons: [], unknown: [], seeders: 0, size_delta: null, verification: passed,
    }
    const reseed = {
      ...job,
      overrides: { source: 'WEB-DL' },
      targets: [target(1, 'ITT', { suggested_action: 'reseed', reseed_torrent_id: '8', dupes: [dead],
                                   tracker_base_url: 'https://itt.example', auto_resurrection: true })],
    } as unknown as UploadJob
    render(<DecisionStep job={reseed} />)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(screen.queryByText(/the reseed revives a torrent with no seeders/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Confirm and queue' })).toBeTruthy()
  })

  it('can schedule the upload for a later time', () => {
    render(<DecisionStep job={{ ...job, overrides: { source: 'WEB-DL' } } as UploadJob} />)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(screen.getByText('At a set time'))
    fireEvent.change(screen.getByLabelText('Start date and time'), { target: { value: '2030-01-02T21:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and schedule' }))

    expect(approve.mock.calls[0][0].scheduled_at).toBe(new Date(2030, 0, 2, 21, 30).toISOString())
  })

  it('blocks the approval while a name is empty', () => {
    render(<DecisionStep job={job} />)

    fireEvent.change(screen.getByLabelText('Release name'), { target: { value: ' ' } })

    // Due volte: nell'elenco per telefono e nella tabella (una delle due nascosta via CSS).
    expect(screen.getAllByText('the release name is empty')).toHaveLength(2)
    expect(screen.getByText('1 tracker(s) still need something before approving.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('warns when the source is nowhere to be found, until it is written', () => {
    render(<DecisionStep job={job} />)
    expect(screen.getByText(/Source not found/)).toBeTruthy()
    cleanup()

    render(<DecisionStep job={{ ...job, overrides: { source: 'BluRay' } } as UploadJob} />)
    expect(screen.queryByText(/Source not found/)).toBeNull()
    cleanup()

    const detected = { ...job.analysis, detected: { group: 'GRP', source: 'WEB-DL' } }
    render(<DecisionStep job={{ ...job, analysis: detected } as UploadJob} />)
    expect(screen.queryByText(/Source not found/)).toBeNull()
  })

  it('warns above the name and in the confirmation without blocking the upload', () => {
    const before = approve.mock.calls.length
    render(<DecisionStep job={job} />)

    expect(screen.getByText(/The name has no source/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(screen.getByText(/No source was found/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Add the source' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Queue anyway' }))

    expect(approve.mock.calls.length).toBe(before + 1)
  })

  it('explains the detected type in a popover on its tag', async () => {
    const analysis = {
      ...job.analysis, detected: { type: 'REMUX', source: 'BluRay', group: 'GRP' },
      type_basis: { type: 'disc_no_encoder', source: 'mediainfo', evidence: ['dv_el', 'pgs'], encoder: null },
    }
    render(<DecisionStep job={{ ...job, analysis } as UploadJob} />)

    fireEvent.click(screen.getByText('REMUX'))

    expect(await screen.findByText(/the video carries no trace of an encoder/)).toBeTruthy()
    expect(screen.getByText(/Dolby Vision profile 7/)).toBeTruthy()
  })

  it('suggests the values the trackers accept in the detected details, still free to write', () => {
    const withOptions = {
      ...job, analysis: { ...job.analysis, field_options: { type: ['REMUX', 'WEBDL'], resolution: ['1080p'] } },
    } as UploadJob
    render(<DecisionStep job={withOptions} />)
    fireEvent.click(screen.getByText('Detected details'))

    const values = Array.from(document.querySelectorAll('#override-options-type option'), (o) => o.getAttribute('value'))
    expect(values).toEqual(['REMUX', 'WEBDL'])
    expect(screen.getByLabelText('Type').getAttribute('list')).toBe('override-options-type')
  })

  it('leaves a wrongly detected group out of the name, and a written value puts it back', () => {
    render(<DecisionStep job={{ ...job, overrides: { source: 'WEB-DL' } } as UploadJob} />)
    fireEvent.click(screen.getByText('Detected details'))

    fireEvent.click(screen.getByRole('button', { name: 'Leave Group out of the name' }))
    expect((screen.getByLabelText('Group') as HTMLInputElement).placeholder).toBe('none: left out of the name')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(updateOverrides.mock.lastCall?.[0]).toEqual({ source: 'WEB-DL', cleared: ['group'] })

    fireEvent.change(screen.getByLabelText('Group'), { target: { value: 'ME' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(updateOverrides.mock.lastCall?.[0]).toEqual({ source: 'WEB-DL', group: 'ME' })
  })

  it('shows a field left out of the name struck through, and can put it back', () => {
    render(<DecisionStep job={{ ...job, overrides: { source: 'WEB-DL', cleared: ['group'] } } as UploadJob} />)

    expect(screen.getByTitle('Group: left out of the name').textContent).toBe('GRP')
    fireEvent.click(screen.getByText('Detected details'))
    fireEvent.click(screen.getByRole('button', { name: 'Put the detected Group back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(updateOverrides.mock.lastCall?.[0]).toEqual({ source: 'WEB-DL' })
  })

  it('lets the generated file name be written by hand, keeping its extension', () => {
    const named = {
      ...job,
      overrides: { source: 'WEB-DL' },
      analysis: {
        ...job.analysis,
        file_names: {
          available: ['generated', 'original'], default: 'generated',
          previews: {
            generated: { name: 'My.Movie.2024.1080p-GRP.mkv', files: ['My.Movie.2024.1080p-GRP.mkv'], count: 1 },
            original: { name: 'My Movie (2024).mkv', files: ['My Movie (2024).mkv'], count: 1 },
          },
        },
      },
    } as unknown as UploadJob
    render(<DecisionStep job={named} />)

    fireEvent.click(screen.getByRole('button', { name: 'Write the name by hand' }))
    const input = screen.getByLabelText('Name in the torrent') as HTMLInputElement
    expect(input.value).toBe('My.Movie.2024.1080p-GRP')
    expect(screen.getByText('.mkv')).toBeTruthy()
    fireEvent.change(input, { target: { value: ' My Movie (2024) [ITA] ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use this name' }))

    expect(updateOverrides.mock.lastCall?.[0]).toEqual({
      source: 'WEB-DL', file_naming: 'generated', content_name: 'My Movie (2024) [ITA]',
    })
  })

  it('goes back to the generated name', () => {
    const named = {
      ...job,
      overrides: { source: 'WEB-DL', file_naming: 'generated', content_name: 'Mine' },
      analysis: {
        ...job.analysis,
        file_names: {
          available: ['generated'], default: 'generated',
          previews: { generated: { name: 'Mine.mkv', files: ['Mine.mkv'], count: 1 } },
        },
      },
    } as unknown as UploadJob
    render(<DecisionStep job={named} />)

    expect(screen.getByText('File name written by hand.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Back to the generated name' }))
    expect(updateOverrides.mock.lastCall?.[0]).toEqual({ source: 'WEB-DL', file_naming: 'generated' })
  })
})
