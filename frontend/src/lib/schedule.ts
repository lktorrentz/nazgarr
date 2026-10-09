import { uiLocale } from '@/lib/i18n'
import { parseApiDate } from '@/lib/time'

// Un upload programmato (scheduled_at, nazgarr/upload/worker.py): il campo è un
// <input type="datetime-local">, in ora locale; al backend va in UTC.

const pad = (n: number) => String(n).padStart(2, '0')

// "2026-10-09T21:00" nell'ora locale, come lo vuole l'input.
export function localInputValue(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

// Il valore dell'input in ISO (UTC), o null se non è una data.
export function fromLocalInput(value: string): string | null {
  const date = new Date(value)
  return value && !Number.isNaN(date.getTime()) ? date.toISOString() : null
}

// La proposta: fra un'ora, al quarto d'ora.
export function defaultSchedule(now: Date = new Date()): string {
  const date = new Date(now.getTime() + 60 * 60 * 1000)
  date.setMinutes(Math.ceil(date.getMinutes() / 15) * 15, 0, 0)
  return localInputValue(date)
}

export function isFuture(iso: string | null | undefined, now: number = Date.now()): boolean {
  return !!iso && parseApiDate(iso).getTime() > now
}

// "gio 9 ott, 21:00"
export function formatWhen(iso: string): string {
  return parseApiDate(iso).toLocaleString(uiLocale(), {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}
