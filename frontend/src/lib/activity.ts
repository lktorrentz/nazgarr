import { useSyncExternalStore } from 'react'

// Feedback delle azioni (approvazioni, ricerche, esclusioni, controlli dei
// recheck), mostrati in basso a destra sopra il popup della run, con lo
// stesso stile. Un piccolo store fuori da React: le mutation lo aggiornano
// dai loro callback, il componente ActivityStack lo legge.
export type ActivityStatus = 'running' | 'success' | 'error' | 'info'

export interface Activity {
  id: string
  status: ActivityStatus
  title: string
  detail?: string
  // 0-100: barra di avanzamento sotto il testo (es. il controllo completo
  // degli hash prima di eseguire un'approvazione).
  progress?: number | null
  createdAt: number
}

// Quanto resta visibile un'attività conclusa; quelle in corso restano finché
// non finiscono, gli errori più a lungo per dare il tempo di leggerli.
const AUTO_DISMISS_MS: Record<ActivityStatus, number | null> = {
  running: null,
  success: 8_000,
  info: 6_000,
  error: 30_000,
}
const MAX_VISIBLE = 5

let activities: Activity[] = []
const listeners = new Set<() => void>()
const timers = new Map<string, ReturnType<typeof setTimeout>>()
let counter = 0

function emit() {
  for (const listener of listeners) listener()
}

function schedule(activity: Activity) {
  const existing = timers.get(activity.id)
  if (existing) clearTimeout(existing)
  const delay = AUTO_DISMISS_MS[activity.status]
  if (delay != null) timers.set(activity.id, setTimeout(() => dismissActivity(activity.id), delay))
}

export function pushActivity(activity: Omit<Activity, 'id' | 'createdAt'>): string {
  const created: Activity = { ...activity, id: `a${++counter}`, createdAt: Date.now() }
  activities = [created, ...activities].slice(0, MAX_VISIBLE)
  schedule(created)
  emit()
  return created.id
}

export function updateActivity(id: string, patch: Partial<Omit<Activity, 'id' | 'createdAt'>>) {
  let updated: Activity | null = null
  activities = activities.map((a) => (a.id === id ? (updated = { ...a, ...patch }) : a))
  if (updated) schedule(updated)
  emit()
}

// Un'attività può essere uscita dallo stack (MAX_VISIBLE) senza che chi la
// aggiorna lo sappia.
export function hasActivity(id: string): boolean {
  return activities.some((a) => a.id === id)
}

export function dismissActivity(id: string) {
  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  activities = activities.filter((a) => a.id !== id)
  emit()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useActivities(): Activity[] {
  return useSyncExternalStore(subscribe, () => activities)
}

// Per i test: stato pulito fra un caso e l'altro.
export function resetActivities() {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  activities = []
  emit()
}
