import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { UploadJob } from '@/api/hooks/uploads'
import { MatchStep } from '@/components/upload/MatchStep'

const confirm = vi.fn()
const split = vi.fn()
const navigate = vi.fn()
// Gli ordinamenti: nessuno di default (i test di prima), o quelli di Lupin.
let ordersData: unknown = undefined

vi.mock('@/api/hooks/settings', () => ({ useSetting: () => ({ data: undefined }) }))
vi.mock('@/api/hooks/uploads', () => ({
  useConfirmMatch: () => ({ mutate: confirm, isPending: false }),
  useSplitUpload: () => ({ mutate: split, isPending: false }),
  useReidentify: () => ({ mutate: vi.fn(), isPending: false }),
  useEpisodeOrders: () => ({ data: ordersData }),
}))
vi.mock('@/api/hooks/metadata', () => ({
  posterUrl: () => '/poster.jpg',
  useMetadataSearch: () => ({ data: undefined, isFetching: false, isError: false }),
  useMetadataDetails: (contentType: string | null) => ({
    isPending: false,
    data:
      contentType === 'tv'
        ? {
            tmdb_id: 95396, content_type: 'tv', title: 'Severance', year: 2022, poster_path: null, genres: ['Drama'],
            runtime: 50, imdb_id: 'tt11280740', tvdb_id: 371980, cast: ['Adam Scott'], original_language: 'en',
            seasons: [
              { season_number: 0, name: 'Specials', episode_count: 3, air_date: null },
              { season_number: 1, name: 'Season 1', episode_count: 9, air_date: null },
              { season_number: 2, name: 'Season 2', episode_count: 10, air_date: null },
            ],
          }
        : undefined,
  }),
}))
vi.mock('@/components/AuthedPoster', () => ({ AuthedPoster: () => null }))
vi.mock('react-router-dom', async (original) => ({ ...(await original<object>()), useNavigate: () => navigate }))

const job = {
  id: 7, status: 'awaiting_match', kind: 'season_pack', is_dir: true, content_type: 'tv', title: 'Severance',
  year: 2022, seasons: [2], episode: null, forced_ids: {},
  candidates: [
    { tmdb_id: 95396, content_type: 'tv', title: 'Severance', year: 2022, poster_path: null, source: 'sonarr' },
    { tmdb_id: 1, content_type: 'movie', title: 'Severance', year: 2006, poster_path: null, source: 'search' },
  ],
  layout: {
    kind: 'season_pack', seasons: [2], episodes_by_season: { '2': [1, 2, 3, 5, 6] },
    videos: [1, 2, 3, 5, 6].map((e) => ({ relative_path: `S02E0${e}.mkv`, size_bytes: 1, season: 2, episodes: [e] })),
  },
} as unknown as UploadJob

afterEach(() => {
  ordersData = undefined
  cleanup()
  confirm.mockClear()
  split.mockClear()
  navigate.mockClear()
})

describe('MatchStep', () => {
  it('suggests the first candidate, compares episodes with TMDB and confirms the season pack', () => {
    render(<MatchStep job={job} />)

    expect(screen.getByText('Sonarr')).toBeTruthy()
    expect(screen.getByText('5/10 episodes')).toBeTruthy()
    expect(screen.getByText('5 missing')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))
    expect(confirm).toHaveBeenCalledWith(
      { content_type: 'tv', tmdb_id: 95396, kind: 'season_pack', seasons: [2], episode: null, episode_order: null },
      expect.anything(),
    )
  })

  it('splits an incomplete season into one upload per episode', () => {
    render(<MatchStep job={job} />)

    fireEvent.click(screen.getByRole('button', { name: 'Split into 5 episodes' }))
    expect(split).toHaveBeenCalledWith(
      { content_type: 'tv', tmdb_id: 95396, kind: 'season_pack', seasons: [2], episode: null, episode_order: null },
      expect.anything(),
    )
    split.mock.calls[0][1].onSuccess({ job_ids: [8, 9, 10, 11, 12] })
    expect(navigate).toHaveBeenCalledWith('/upload')
  })

  it('does not offer the split for a complete season', () => {
    const complete = {
      ...job,
      layout: { ...(job.layout as object), episodes_by_season: { '2': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] } },
    } as unknown as UploadJob
    render(<MatchStep job={complete} />)

    expect(screen.queryByRole('button', { name: /^Split into/ })).toBeNull()
  })

  it('offers the specials (season 0) last, even when the source does not say so', () => {
    render(<MatchStep job={job} />)

    const seasons = screen.getAllByRole('radio').filter((s) => /Specials|Season \d/.test(s.textContent ?? ''))
    expect(seasons.map((s) => s.textContent?.match(/Specials|Season \d/)?.[0])).toEqual(['Season 1', 'Season 2', 'Specials'])
    fireEvent.click(seasons[2])
    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))
    expect(confirm.mock.calls.at(-1)?.[0]).toMatchObject({ kind: 'season_pack', seasons: [0] })
  })

  it('switches to a movie candidate and sends it as a movie', () => {
    render(<MatchStep job={job} />)

    fireEvent.click(screen.getAllByRole('button', { pressed: false })[0])
    expect(screen.getByText('The files look like episodes of a series, but a movie is selected.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))
    expect(confirm.mock.calls[0][0]).toMatchObject({ content_type: 'movie', tmdb_id: 1, kind: 'movie', seasons: [] })
  })

  it('says how sure the best match is against the automatic threshold, and why', async () => {
    const scored = {
      ...job,
      candidates: [
        { ...job.candidates[0], source: 'search', confidence: 0.85,
          confidence_parts: { basis: 'name', title: 1, title_guess: 'Severance', title_matched: 'Severance', year: 0.85,
            year_guess: 2021, year_candidate: 2022, type: 1, type_guess: 'tv', type_candidate: 'tv' } },
        job.candidates[1],
      ],
    } as unknown as UploadJob
    render(<MatchStep job={scored} />)

    // In alto a destra solo la percentuale; il perché nel popover.
    const badge = screen.getByRole('button', { name: 'Reliability 85%.' })
    expect(badge.textContent).toBe('85%')
    fireEvent.click(badge)
    expect(await screen.findByText(/Reliability 85%\./)).toBeTruthy()
    expect(screen.getByText(/Below the automatic match threshold \(90%\)/)).toBeTruthy()
    // Fattore per fattore, con il motivo, e il prodotto.
    expect(screen.getByText('file 2021, TMDB 2022: one year apart (often release vs. name)')).toBeTruthy()
    expect(screen.getByText('"Severance" from the file, closest TMDB title "Severance"')).toBeTruthy()
    expect(screen.getByText('100% × 85% × 100% = 85%')).toBeTruthy()
    expect(screen.queryByText(/Best match/)).toBeNull()
  })

  it('picks the ordering that fits the files, warns when it is not TVDB aired, and sends the choice', () => {
    const episode = (n: number, refs: number[][]) => ({ number: n, titles: [`Capitolo ${n}`], air_date: null, refs })
    const parts = {
      key: 'tmdb:group:g1', label: 'TMDB · Parts', source: 'tmdb',
      seasons: [
        { season_number: 1, episodes: [1, 2, 3, 4, 5].map((n) => episode(n, [[1, n]])) },
        { season_number: 2, episodes: [1, 2, 3, 4, 5].map((n) => episode(n, [[1, n + 5]])) },
      ],
    }
    const aired = {
      key: 'sonarr:aired', label: 'TVDB · Aired (sonarr)', source: 'sonarr',
      seasons: [{ season_number: 1, episodes: Array.from({ length: 10 }, (_, i) => episode(i + 1, [[1, i + 1]])) }],
    }
    ordersData = {
      orders: [parts, aired], recommended: 'tmdb:group:g1', files_order: 'tmdb:group:g1',
      fits: { 'tmdb:group:g1': { score: 1, matched: 5, files: 5, complete_seasons: 1 },
              'sonarr:aired': { score: 0.85, matched: 5, files: 5, complete_seasons: 0 } },
      warning: { code: 'files_not_tvdb_aired', order: 'tmdb:group:g1', tvdb: 'sonarr:aired' },
      found: { 'tmdb:group:g1': { '2': [1, 2, 3, 4, 5] }, 'sonarr:aired': { '1': [6, 7, 8, 9, 10] } },
    }
    render(<MatchStep job={job} />)

    expect(screen.getByText('Episode ordering')).toBeTruthy()
    expect(screen.getByText(/do not follow TVDB · Aired \(sonarr\)/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))
    expect(confirm.mock.calls.at(-1)?.[0]).toMatchObject({ seasons: [2], episode_order: 'tmdb:group:g1' })

    // Con la scorciatoia si passa a TVDB aired: la stagione dei file diventa la 1,
    // e la corrispondenza dice che S02E01 dei file è S01E06.
    fireEvent.click(screen.getByRole('button', { name: 'Use TVDB · Aired (sonarr)' }))
    fireEvent.click(screen.getByText('How the files map (5 episodes)'))
    expect(screen.getByText('S01E06')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))
    expect(confirm.mock.calls.at(-1)?.[0]).toMatchObject({ seasons: [1], episode_order: 'sonarr:aired' })
  })
})
