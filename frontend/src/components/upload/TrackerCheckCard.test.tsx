import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { UploadJob, UploadTarget } from '@/api/hooks/uploads'
import { TrackerCheckCard } from '@/components/upload/TrackerCheckCard'

const verify = vi.fn()
vi.mock('@/api/hooks/uploads', () => ({ useVerifyTarget: () => ({ mutate: verify, isPending: false }) }))

const dupe = (id: string, verdict: string, extra = {}) => ({
  torrent_id_remote: id, name: `Release.${id}`, size_bytes: 1e9, verdict, score: null, reasons: [], unknown: [],
  seeders: null, size_delta: null, verification: null, ...extra,
})

const target = {
  id: 3, tracker_label: 'ITT', status: 'awaiting_decision', suggested_action: 'reseed', error_message: null,
  dupes: [
    dupe('1', 'identical'),
    dupe('2', 'same_slot', { reasons: ['covered_by_pack'] }),
    dupe('3', 'different', { reasons: ['resolution'] }),
  ],
} as unknown as UploadTarget
const job = { id: 9, status: 'awaiting_decision', targets: [target] } as unknown as UploadJob

describe('TrackerCheckCard', () => {
  it('shows each result with its verdict and runs the full hash check on the identical one', () => {
    render(<TrackerCheckCard job={job} target={target} />)

    expect(screen.getByText('Identical')).toBeTruthy()
    expect(screen.getByText('Dupe')).toBeTruthy()
    expect(screen.getByText('season pack that includes it')).toBeTruthy()
    expect(screen.getByText('other resolution')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Full hash check' }))
    expect(verify).toHaveBeenCalledWith({ targetId: 3, torrentIdRemote: '1' }, expect.anything())
  })

  it('shows a passed check instead of the button', () => {
    const verified = {
      ...target,
      dupes: [dupe('1', 'identical', { verification: { status: 'passed', reason: null, ok: 40, pieces: 40 } })],
    } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={verified} />)

    expect(screen.getByText('Every piece matches (40): reseed instead of uploading.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Full hash check' })).toBeNull()
  })

  it('leaves the size out when the tracker did not give one', () => {
    const sizeless = { ...target, dupes: [dupe('1', 'different', { size_bytes: null })] } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={sizeless} />)
    expect(screen.getByText('Release.1')).toBeTruthy()
    expect(screen.queryByText(/null/)).toBeNull()
  })

  it('says when the tracker had more results than were read', () => {
    const truncated = { ...job, analysis: { dupes_truncated: { '3': 1000 } } } as unknown as UploadJob
    render(<TrackerCheckCard job={truncated} target={target} />)
    expect(screen.getByText(/more results than were read \(1000\)/)).toBeTruthy()
    cleanup()

    render(<TrackerCheckCard job={job} target={target} />)
    expect(screen.queryByText(/more results than were read/)).toBeNull()
  })

  it('shows the score, what is not known, a dead dupe and a possible trump', () => {
    const scored = {
      ...target,
      dupes: [
        dupe('4', 'possible', { score: 44, unknown: ['codec', 'languages'] }),
        dupe('5', 'same_slot', { score: 100, seeders: 0 }),
        dupe('6', 'trump', { score: 100, reasons: ['size_gap'], size_delta: 0.25, seeders: 3 }),
      ],
    } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={scored} />)

    expect(screen.getByText('Possible dupe')).toBeTruthy()
    expect(screen.getByText('44%')).toBeTruthy()
    expect(screen.getByText('not known: codec, audio languages')).toBeTruthy()
    expect(screen.getByText('no seeders: your upload replaces it')).toBeTruthy()
    expect(screen.getByText('another encode (+25%)')).toBeTruthy()
    expect(screen.getByText('3 seed')).toBeTruthy()
    expect(screen.getByText(/too little is known to call it a dupe/)).toBeTruthy()
    expect(screen.getByText(/a different encode: upload it only if it is better/)).toBeTruthy()
  })

  it('asks to claim the resurrection of a dead identical torrent once its check passed', () => {
    const onClaimedChange = vi.fn()
    const passed = { status: 'passed', reason: null, ok: 40, pieces: 40 }
    const dead = { ...target, dupes: [dupe('1', 'identical', { seeders: 0, verification: passed })] } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={dead} onClaimedChange={onClaimedChange} />)

    expect(screen.getByText(/claim its resurrection on the tracker before the reseed starts/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Claimed' }))
    expect(onClaimedChange).toHaveBeenCalledWith(true)
    cleanup()

    // Prima del controllo completo, nessun invito: il reseed non è ancora possibile.
    const unchecked = { ...target, dupes: [dupe('1', 'identical', { seeders: 0 })] } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={unchecked} onClaimedChange={onClaimedChange} />)
    expect(screen.queryByText(/claim its resurrection/)).toBeNull()
  })

  it('says Nazgarr claims the resurrection when the tracker has a website session', () => {
    const passed = { status: 'passed', reason: null, ok: 40, pieces: 40 }
    const dead = {
      ...target, auto_resurrection: true, dupes: [dupe('1', 'identical', { seeders: 0, verification: passed })],
    } as unknown as UploadTarget
    render(<TrackerCheckCard job={job} target={dead} onClaimedChange={vi.fn()} />)

    expect(screen.getByText(/Nazgarr claims its resurrection on the tracker with your website session/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Claimed' })).toBeNull()
  })

  it("warns when no audio track is in the tracker's language", () => {
    const withLanguage = {
      ...job,
      analysis: { languages: { '7': { language: 'it', status: 'missing' } } },
    } as unknown as UploadJob
    render(<TrackerCheckCard job={withLanguage} target={{ ...target, tracker_id: 7 } as UploadTarget} />)
    expect(screen.getByText(/No audio track in Italian/)).toBeTruthy()
    cleanup()

    const fine = { ...job, analysis: { languages: { '7': { language: 'it', status: 'present' } } } } as unknown as UploadJob
    render(<TrackerCheckCard job={fine} target={{ ...target, tracker_id: 7 } as UploadTarget} />)
    expect(screen.queryByText(/audio track/)).toBeNull()
  })
})
