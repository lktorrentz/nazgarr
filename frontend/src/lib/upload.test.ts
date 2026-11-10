import { describe, expect, it } from 'vitest'

import {
  draftProblem,
  editDraft,
  executionSteps,
  effectiveDraft,
  eventMessage,
  fromForcedIds,
  commonFolder,
  missingEpisodes,
  newUploadLink,
  parseNewUploadParams,
  plannedMediaInfo,
  toForcedIds,
} from '@/lib/upload'

describe('missingEpisodes', () => {
  it('lists the episodes TMDB expects that are not in the source', () => {
    expect(missingEpisodes([1, 2, 4], 5)).toEqual([3, 5])
    expect(missingEpisodes([1, 2, 3], 3)).toEqual([])
  })
})

describe('forced ids', () => {
  it('drops empty fields and keeps only numeric TVDB/MAL ids', () => {
    expect(toForcedIds({ tmdb: ' tv/1399 ', imdb: '', tvdb: '121361', mal: 'abc' })).toEqual({
      tmdb: 'tv/1399',
      imdb: undefined,
      tvdb: 121361,
      mal: undefined,
    })
  })

  it('round-trips what the backend stored', () => {
    expect(fromForcedIds({ tmdb: '603', tvdb: 5 })).toEqual({ tmdb: '603', imdb: '', tvdb: '5', mal: '' })
  })
})

describe('eventMessage', () => {
  it('uses the upload event sentence, then the error dictionary', () => {
    expect(eventMessage({ code: 'match_confirmed', params: { title: 'Dune', year: 2021 } })).toBe(
      'Match confirmed: Dune (2021).',
    )
    expect(eventMessage({ code: 'no_video_files', params: {} })).toBe('No video file found in the source.')
  })

  it('turns a coded error inside a failure into its sentence', () => {
    const message = eventMessage({
      code: 'reseed_failed',
      params: { error: 'upload_resurrection_failed', tracker: 'ITT', reason: 'HTTP 503' },
    })
    expect(message).toMatch(/^Reseed failed: ITT: the resurrection claim did not go through \(HTTP 503\)/)
    // Un testo libero resta com'è.
    expect(eventMessage({ code: 'reseed_failed', params: { error: 'connection refused' } })).toBe(
      'Reseed failed: connection refused',
    )
  })
})

const source = {
  suggested_action: 'upload', proposed_name: 'Movie (2024) 1080p-GRP', category_id: 1, type_id: 4, resolution_id: 3,
  flags: { anonymous: false }, reseed_torrent_id: null, dupes: [{ torrent_id_remote: '9', verdict: 'identical' as const }],
}

describe('decision drafts', () => {
  it('follows the proposed values until the user touches a field', () => {
    const edited = editDraft(effectiveDraft(undefined, source), { name: 'Mine' })
    const refreshed = effectiveDraft(edited, { ...source, proposed_name: 'Movie (2024) 1080p-ME', type_id: 5 })
    expect(refreshed.name).toBe('Mine')
    expect(refreshed.type_id).toBe(5)
    expect(refreshed.reseed_torrent_id).toBe('9')
  })

  it('explains what is missing', () => {
    const draft = effectiveDraft(undefined, source)
    expect(draftProblem(draft)).toBeNull()
    expect(draftProblem({ ...draft, type_id: null })).toBe('upload.decision.problem.ids')
    expect(draftProblem({ ...draft, action: 'reseed', reseed_torrent_id: null })).toBe('upload.decision.problem.reseed')
    expect(draftProblem({ ...draft, action: 'skip', name: '' })).toBeNull()
  })
})

describe('new upload link from the poster view', () => {
  it('carries the source and the TMDB id, and reads them back', () => {
    const link = newUploadLink({ diskId: 2, path: 'media/TV/Show/Season 01', isDir: true }, 'tv/1399')
    expect(link).toBe('/upload/new?disk=2&path=media%2FTV%2FShow%2FSeason+01&dir=1&tmdb=tv%2F1399')

    const parsed = parseNewUploadParams(new URLSearchParams(link.split('?')[1]))
    expect(parsed).toEqual({
      source: { diskId: 2, relativePath: 'media/TV/Show/Season 01', isDir: true }, tmdb: 'tv/1399', trackers: null,
    })
    expect(parseNewUploadParams(new URLSearchParams(''))).toEqual({ source: null, tmdb: '', trackers: null })
    const forItt = newUploadLink({ diskId: 1, path: 'm.mkv', isDir: false }, 'movie/1', [3])
    expect(parseNewUploadParams(new URLSearchParams(forItt.split('?')[1])).trackers).toEqual([3])
  })
})

describe('commonFolder', () => {
  it('finds the folder holding every file of a series, on one disk', () => {
    const f = (disk_id: number, relative_path: string) => ({ disk_id, relative_path })
    expect(commonFolder([f(1, 'tv/Show/S01/a.mkv'), f(1, 'tv/Show/S02/b.mkv')])).toEqual({ diskId: 1, path: 'tv/Show' })
    expect(commonFolder([f(1, 'tv/Show/S01/a.mkv'), f(2, 'tv/Show/S02/b.mkv')])).toBeNull()
    expect(commonFolder([f(1, 'a.mkv'), f(1, 'b.mkv')])).toBeNull()
  })
})

describe('executionSteps', () => {
  const targets = [
    { id: 1, tracker_label: 'ITT', action: 'upload', status: 'approved' },
    { id: 2, tracker_label: 'BLU', action: 'reseed', status: 'approved' },
    { id: 3, tracker_label: 'OTH', action: 'skip', status: 'skipped' },
  ]
  const states = (job: Parameters<typeof executionSteps>[0]) =>
    executionSteps(job).map((step) => `${step.key}:${step.state}`)

  it('keeps the finished steps done while the next one runs', () => {
    expect(states({ status: 'running', stage: 'screenshots', events: [{ code: 'torrent_created' }], targets })).toEqual([
      'hashing:done', 'screenshots:active', 'target-1:pending', 'target-2:pending', 'finish:pending',
    ])
    const onBlu = [{ ...targets[0], status: 'done' }, { ...targets[1], status: 'preparing' }, targets[2]]
    expect(states({ status: 'running', stage: 'tracker:BLU', events: [], targets: onBlu })).toEqual([
      'hashing:done', 'screenshots:done', 'target-1:done', 'target-2:active', 'finish:pending',
    ])
  })

  it('marks a failed hashing and skips the screenshots, reseeds go on', () => {
    const failed = [{ ...targets[0], status: 'failed' }, { ...targets[1], status: 'seeding' }, targets[2]]
    expect(states({ status: 'running', stage: 'tracker:BLU', events: [], targets: failed })).toEqual([
      'hashing:failed', 'screenshots:skipped', 'target-1:failed', 'target-2:active', 'finish:pending',
    ])
  })

  it('has no torrent or screenshot steps for reseeds only', () => {
    const reseed = [{ ...targets[1], status: 'done' }]
    expect(states({ status: 'done', stage: null, events: [], targets: reseed })).toEqual(['target-2:done', 'finish:done'])
  })
})

describe('client labels in the draft', () => {
  const target = {
    suggested_action: 'upload', proposed_name: 'X', category_id: 1, type_id: 1, resolution_id: 1, flags: {},
    reseed_torrent_id: null, dupes: [{ torrent_id_remote: '7', verdict: 'identical' as const }],
    client_defaults: { category: 'movie', tags_upload: 'release', tags_reseed: 'reseed' },
  }

  it('starts from the client defaults and the tags follow the action', () => {
    const draft = effectiveDraft(undefined, target)
    expect([draft.client_category, draft.client_tags]).toEqual(['movie', 'release'])
    const reseed = effectiveDraft(editDraft(draft, { action: 'reseed' }), target)
    expect(reseed.client_tags).toBe('reseed')
    // Tag scritti a mano: restano anche cambiando azione.
    const mine = editDraft(editDraft(draft, { client_tags: 'mine' }), { action: 'reseed' })
    expect(effectiveDraft(mine, target).client_tags).toBe('mine')
  })
})

describe('reseed needs a passed hash check', () => {
  const base = {
    suggested_action: 'reseed', proposed_name: 'X', category_id: 1, type_id: 1, resolution_id: 1, flags: {},
    reseed_torrent_id: null,
  }
  it('is a problem until the check passed', () => {
    const unverified = effectiveDraft(undefined, { ...base, dupes: [{ torrent_id_remote: '7', verdict: 'identical' }] })
    expect(draftProblem(unverified)).toBe('upload.decision.problem.verify')
    const passed = effectiveDraft(undefined, {
      ...base, dupes: [{ torrent_id_remote: '7', verdict: 'identical', verification: { status: 'passed' } }],
    })
    expect(draftProblem(passed)).toBeNull()
  })
})

describe('plannedMediaInfo', () => {
  const text = 'General\nComplete name                            : /data/media/My Movie (2024).mkv\nFormat : Matroska\n'
  const analysis = {
    file_names: {
      available: ['generated', 'original'], default: 'generated',
      previews: {
        generated: { name: 'My.Movie.2024.mkv', files: [], count: 1, main_video: 'My.Movie.2024.mkv' },
        original: { name: 'My Movie (2024).mkv', files: [], count: 1, main_video: 'My Movie (2024).mkv' },
      },
    },
  }

  it('names the file as the tracker will see it, with the chosen file names', () => {
    expect(plannedMediaInfo({ analysis, overrides: {}, mediainfo_text: text })).toBe(
      'General\nComplete name                            : My.Movie.2024.mkv\nFormat : Matroska\n')
    expect(plannedMediaInfo({ analysis, overrides: { file_naming: 'original' }, mediainfo_text: text })).toContain(
      ': My Movie (2024).mkv\n')
  })

  it('leaves the text alone without a planned name', () => {
    expect(plannedMediaInfo({ analysis: {}, overrides: {}, mediainfo_text: text })).toBe(text)
    expect(plannedMediaInfo({ analysis, overrides: {}, mediainfo_text: null })).toBeNull()
  })
})
