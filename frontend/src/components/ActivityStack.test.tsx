import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ActivityStack } from '@/components/ActivityStack'
import { resetActivities } from '@/lib/activity'

let checks: unknown[] = []
vi.mock('@/api/hooks/fullChecks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/hooks/fullChecks')>()),
  useFullChecks: () => ({ data: checks }),
}))
vi.mock('@/api/hooks/reviews', () => ({ useRecentSeedJobs: () => ({ data: [] }) }))
// Stabile come quello vero: l'effetto del watcher dipende dal client.
const queryClient = vi.hoisted(() => ({ invalidateQueries: () => undefined }))
vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => queryClient }))

const check = (id: string, status: string, over: object = {}) => ({
  id, status, purpose: 'verify', label: `${id}.mkv`, bytes_total: null, bytes_done: 0, stage: 'verifying',
  verdict: null, verdict_reason: null, execution: null, execution_error: null, error: null, ...over,
})

describe('ActivityStack verification watcher', () => {
  afterEach(() => act(() => resetActivities()))

  it('groups the queued checks in one notice and gives the running one its own', () => {
    checks = [check('a', 'running'), check('b', 'queued'), check('c', 'queued'), check('d', 'queued')]
    render(<ActivityStack />)
    expect(screen.getByText('3 hash checks queued')).toBeTruthy()
    expect(screen.getByText('a.mkv')).toBeTruthy()
    expect(screen.queryByText('b.mkv')).toBeNull()
  })

  it('announces the outcome of a queued check that finished between two reads', () => {
    checks = [check('e', 'queued')]
    const { rerender } = render(<ActivityStack />)
    checks = [check('e', 'done', { verdict: 'failed', verdict_reason: 'pieces differ' })]
    rerender(<ActivityStack />)
    expect(screen.queryByText(/hash checks queued/)).toBeNull()
    expect(screen.getByText('pieces differ')).toBeTruthy()
  })
})
