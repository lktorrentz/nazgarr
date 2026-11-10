import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ReseedingPage } from '@/pages/reseeding/ReseedingPage'

const approve = vi.fn()
const reject = vi.fn()
const fetchNextPage = vi.fn()
const retry = vi.fn()
let reviews: unknown[] = []
let seedJobs: unknown[] = []
let hasNextPage = false
let queueError: Error | null = null
const refetch = vi.fn()
const toast = vi.hoisted(() => ({ info: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast }))

const review = (id: number, name: string) => ({
  id, candidate_name: name, direction: 'media_to_torrent', confidence: 0.97, ambiguity_reason: null,
  status: 'pending', format: 'single', tracker_id: 1, tracker: 'T', tmdb_id: id, season_number: null,
  episode_number: null, media_item_id: id, layout: null, verify_status: null, seeding_on: [], seeding_here: [],
  torrent_client_id: null, client_category: null, client_tags: null,
})

vi.mock('@/api/hooks/reviews', () => ({
  useReviewQueue: () =>
    queueError
      ? { data: undefined, isPending: false, isSuccess: false, isError: true, error: queueError, isFetching: false, refetch,
          hasNextPage: false, isFetchingNextPage: false, fetchNextPage }
      : {
          data: { pages: [{ items: reviews, total: reviews.length, offset: 0, next_offset: hasNextPage ? reviews.length : null }] },
          isPending: false, isSuccess: true, isError: false, error: null, isFetching: false, refetch,
          hasNextPage, isFetchingNextPage: false, fetchNextPage,
        },
  useApproveReview: () => ({ mutate: approve, isPending: false }),
  useRejectReview: () => ({ mutate: reject, isPending: false }),
  useDecideReviews: () => ({ mutate: vi.fn(), isPending: false }),
  useCandidateAudit: () => ({ data: [], isPending: false }),
  useSetReviewClientLabels: () => ({ mutate: vi.fn(), isPending: false }),
  useRecentSeedJobs: () => ({ data: seedJobs, isPending: false }),
  useRetryFailed: () => ({ mutate: retry, isPending: false }),
  useDeleteSeedJob: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/api/hooks/fullChecks', () => ({
  useFullCheck: () => ({ data: undefined }),
  useStartFullCheck: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelFullCheck: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('@/api/hooks/torrentClients', () => ({ useTorrentClientCategories: () => ({ data: undefined }) }))

describe('ReseedingPage review queue', () => {
  it('says the queue is empty only when it was read', () => {
    reviews = []
    render(<ReseedingPage />)
    expect(screen.getByText('No pending reviews.')).toBeTruthy()
  })

  it('shows a queue that failed to load as an error with Retry, not as an empty queue', () => {
    queueError = new Error('Service Unavailable')
    try {
      render(<ReseedingPage />)
      expect(screen.queryByText('No pending reviews.')).toBeNull()
      expect(screen.getByText('This list could not be loaded.')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(refetch).toHaveBeenCalledTimes(1)
    } finally {
      queueError = null
    }
  })

  it('approves and rejects the review of the row clicked', () => {
    reviews = [review(1, 'First.Movie.mkv'), review(2, 'Second.Movie.mkv')]
    render(<ReseedingPage />)
    const approveButtons = screen.getAllByRole('button', { name: 'Approve' })
    fireEvent.click(approveButtons[1])
    expect(approve).toHaveBeenCalledWith(2)
    fireEvent.click(screen.getAllByRole('button', { name: 'Reject' })[0])
    expect(reject).toHaveBeenCalledWith(1)
  })

  it('asks the server for the next page when the end of the queue comes into view', () => {
    const observed: IntersectionObserverCallback[] = []
    class FakeObserver {
      constructor(callback: IntersectionObserverCallback) {
        observed.push(callback)
      }
      observe() {}
      disconnect() {}
    }
    const original = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = FakeObserver as unknown as typeof IntersectionObserver
    try {
      reviews = Array.from({ length: 100 }, (_, i) => review(i + 1, `Movie.${i}.mkv`))
      hasNextPage = true
      render(<ReseedingPage />)
      // Tutta la pagina ricevuta, e il totale della coda in testa.
      expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(100)
      expect(screen.getByText('100 waiting for a decision')).toBeTruthy()
      observed.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver)
      expect(fetchNextPage).toHaveBeenCalledTimes(1)
    } finally {
      globalThis.IntersectionObserver = original
      hasNextPage = false
    }
  })
})

describe('ReseedingPage executions', () => {
  it('says a retry that asked for a recheck is waiting for it, not completed', () => {
    reviews = []
    seedJobs = [{
      id: 7, candidate_id: 3, candidate_name: 'Movie.mkv', direction: 'media_to_torrent', tracker: 'T',
      torrent_client: 'qb', final_status: 'failed', display_status: null, recheck_status: 'failed',
      recheck_skipped: false, error_message: 'Recheck failed, client state: stoppedDL', hardlink_created_at: null,
      torrent_added_at: null,
    }]
    try {
      render(<ReseedingPage />)
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(retry).toHaveBeenCalledWith(7, expect.anything())
      const { onSuccess } = retry.mock.calls[0][1]
      onSuccess({ final_status: 'in_progress', recheck_status: 'pending', verify_check_id: null })
      expect(toast.info).toHaveBeenCalledWith('The client is checking it again: it starts only if the recheck passes.')
      onSuccess({ final_status: 'seeding', recheck_status: 'ok', verify_check_id: null })
      expect(toast.success).toHaveBeenCalledWith('Retry completed.')
    } finally {
      seedJobs = []
    }
  })

  it('shows the resurrection claimed before the add', () => {
    reviews = []
    seedJobs = [{
      id: 8, candidate_id: 4, candidate_name: 'Dead.mkv', direction: 'media_to_torrent', tracker: 'T',
      torrent_client: 'qb', final_status: 'seeding', display_status: null, recheck_status: 'ok',
      recheck_skipped: false, resurrection: 'claimed', error_message: null, hardlink_created_at: null,
      torrent_added_at: null,
    }]
    try {
      render(<ReseedingPage />)
      expect(screen.getByText('resurrection claimed')).toBeTruthy()
    } finally {
      seedJobs = []
    }
  })
})
