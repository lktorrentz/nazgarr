import { describe, expect, it } from 'vitest'

import { defaultSchedule, fromLocalInput, isFuture, localInputValue } from '@/lib/schedule'

describe('schedule', () => {
  it('goes from the local time of the field to UTC and back', () => {
    const value = localInputValue(new Date(2026, 9, 9, 21, 5))
    expect(value).toBe('2026-10-09T21:05')
    expect(new Date(fromLocalInput(value)!).getTime()).toBe(new Date(2026, 9, 9, 21, 5).getTime())
    expect(fromLocalInput('')).toBeNull()
  })

  it('proposes an hour from now, on the quarter hour', () => {
    expect(defaultSchedule(new Date(2026, 9, 9, 20, 7))).toBe('2026-10-09T21:15')
    expect(defaultSchedule(new Date(2026, 9, 9, 20, 50))).toBe('2026-10-09T22:00')
  })

  it('tells a time to come from one already gone', () => {
    const now = Date.UTC(2026, 9, 9, 12)
    expect(isFuture('2026-10-09T13:00:00', now)).toBe(true) // da SQLite, senza fuso: UTC
    expect(isFuture('2026-10-09T11:00:00+00:00', now)).toBe(false)
    expect(isFuture(null, now)).toBe(false)
  })
})
