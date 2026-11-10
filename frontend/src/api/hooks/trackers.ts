import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'

import { api, ApiError, unwrap } from '@/api/client'
import type { Schemas } from '@/api/client'
import { TOAST_ERRORS } from '@/lib/mutationErrors'

export function useTrackers() {
  return useQuery({
    queryKey: ['trackers'],
    queryFn: () => unwrap(api.GET('/api/trackers')),
  })
}

export function useCreateTracker() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Schemas['TrackerCreateRequest']) => unwrap(api.POST('/api/trackers', { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers'] }),
  })
}

export function useUpdateTracker() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: Schemas['TrackerUpdateRequest'] }) =>
      unwrap(api.PATCH('/api/trackers/{tracker_id}', { params: { path: { tracker_id: id } }, body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers'] }),
  })
}

// Se il cookie della sessione web vale ancora (una pagina del sito in sola
// lettura): per il claim automatico della resurrection (2026-11-10).
export function useCheckTrackerSession() {
  return useMutation({
    mutationFn: (id: number) =>
      unwrap(api.POST('/api/trackers/{tracker_id}/session-check', { params: { path: { tracker_id: id } } })),
  })
}

// Cosa se ne va dal DB eliminando il tracker (decisione dell'utente,
// 2026-10-18): letto quando si apre la conferma, mai tenuto in cache. Il
// pulsante resta montato a conferma chiusa, quindi gcTime 0 non basta: alla
// chiusura il conteggio si butta, così riaprendo non si mostra (né si
// conferma) quello vecchio mentre arriva il nuovo.
export function useTrackerDeletePreview(id: number, enabled: boolean) {
  const queryClient = useQueryClient()
  useEffect(() => {
    if (!enabled) queryClient.removeQueries({ queryKey: ['trackers', id, 'delete-preview'], exact: true })
  }, [enabled, id, queryClient])
  return useQuery({
    queryKey: ['trackers', id, 'delete-preview'],
    queryFn: () =>
      unwrap(api.GET('/api/trackers/{tracker_id}/delete-preview', { params: { path: { tracker_id: id } } })),
    enabled,
    staleTime: 0,
    gcTime: 0,
  })
}

export function useDeleteTracker() {
  const queryClient = useQueryClient()
  return useMutation({
    meta: TOAST_ERRORS,
    mutationFn: (id: number) => unwrap(api.DELETE('/api/trackers/{tracker_id}', { params: { path: { tracker_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers'] }),
  })
}

export function useBundledUploadProfiles() {
  return useQuery({
    queryKey: ['trackers', 'upload-profiles', 'bundled'],
    queryFn: () => unwrap(api.GET('/api/trackers/upload-profiles/bundled')),
  })
}

export function useUploadProfile(trackerId: number) {
  return useQuery({
    queryKey: ['trackers', trackerId, 'upload-profile'],
    // 404 = il tracker non ha ancora un profilo: un valore, non un errore.
    queryFn: async () => {
      try {
        return await unwrap(api.GET('/api/trackers/{tracker_id}/upload-profile', {
          params: { path: { tracker_id: trackerId } },
        }))
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null
        throw error
      }
    },
  })
}

export function useCreateUploadProfile(trackerId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    meta: TOAST_ERRORS,
    mutationFn: (body: Schemas['UploadProfileCreateRequest']) =>
      unwrap(api.POST('/api/trackers/{tracker_id}/upload-profile', { params: { path: { tracker_id: trackerId } }, body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers', trackerId, 'upload-profile'] }),
  })
}

export function useUpdateUploadProfile(trackerId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Schemas['UploadProfileUpdateRequest']) =>
      unwrap(api.PATCH('/api/trackers/{tracker_id}/upload-profile', { params: { path: { tracker_id: trackerId } }, body })),
    // Anche gli upload: le opzioni del profilo (freeleech, mappe) arrivano
    // sui target di un job già aperto e nella pagina di creazione.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
      return queryClient.invalidateQueries({ queryKey: ['trackers', trackerId, 'upload-profile'] })
    },
  })
}

export function useDeleteUploadProfile(trackerId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    meta: TOAST_ERRORS,
    mutationFn: () =>
      unwrap(api.DELETE('/api/trackers/{tracker_id}/upload-profile', { params: { path: { tracker_id: trackerId } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers', trackerId, 'upload-profile'] }),
  })
}

// Il profilo incluso (quello d'origine, o quello scelto) al posto di quello
// che c'è, modifiche comprese. Anche la scheda del tracker cambia.
export function useRestoreUploadProfile(trackerId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (profileKey: string | null) =>
      unwrap(
        api.POST('/api/trackers/{tracker_id}/upload-profile/restore', {
          params: { path: { tracker_id: trackerId } },
          body: { profile_key: profileKey },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers'] }),
  })
}

export function useUpdateNamingFromBundled(trackerId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      unwrap(
        api.POST('/api/trackers/{tracker_id}/upload-profile/naming/update', { params: { path: { tracker_id: trackerId } } }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trackers', trackerId, 'upload-profile'] }),
  })
}

export interface NamingPreview {
  sample: { kind: 'job' | 'example'; label: string | null }
  variables: Record<string, string | null>
  names: Record<string, string>
  // Il nome finale di ogni esempio fisso e, se c'è, dell'ultimo upload analizzato (per primo).
  examples: { kind: 'job' | 'example'; key: string; label: string; name: string }[]
}

// Anteprima delle regole di naming ancora da salvare (debounce a carico del chiamante).
export function useNamingPreview(trackerId: number, rules: Record<string, unknown> | null) {
  return useQuery({
    queryKey: ['trackers', trackerId, 'naming-preview', rules],
    queryFn: async () =>
      (await unwrap(
        api.POST('/api/trackers/{tracker_id}/upload-profile/naming/preview', {
          params: { path: { tracker_id: trackerId } },
          body: { naming_rules: rules! },
        }),
      )) as unknown as NamingPreview,
    enabled: rules !== null,
    placeholderData: (previous) => previous,
    retry: false,
  })
}
