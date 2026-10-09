import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, unwrap } from '@/api/client'
import type { Schemas } from '@/api/client'
import type { NamingPreview } from '@/api/hooks/trackers'

export type UploadJob = Schemas['UploadJobDetail']
export type UploadJobSummary = Schemas['UploadJobSummary']
export type UploadTarget = Schemas['UploadTargetResponse']

// Stati in cui il worker sta lavorando (nazgarr/upload/jobs.py WORKER_STATES):
// solo lì serve il polling, a un punto di approvazione o a job finito no.
export const WORKER_STATES = ['identifying', 'analyzing', 'queued', 'running']
const POLL_MS = 2000

// Anche a un punto di approvazione un target può avere un full hash check in corso.
const TARGET_WORKING_STATES = ['checking', 'verifying', 'preparing', 'uploading', 'seeding']

function isWorking(status: string | undefined) {
  return status !== undefined && WORKER_STATES.includes(status)
}

function isJobWorking(job: UploadJobSummary | undefined) {
  return !!job && (isWorking(job.status) || job.targets.some((target) => TARGET_WORKING_STATES.includes(target.status)))
}

export function useUploads() {
  return useQuery({
    queryKey: ['uploads'],
    queryFn: () => unwrap(api.GET('/api/uploads')),
    refetchInterval: (query) => (query.state.data?.some(isJobWorking) ? POLL_MS : false),
  })
}

export function useUpload(uploadId: number | null) {
  return useQuery({
    queryKey: ['uploads', uploadId],
    queryFn: () => unwrap(api.GET('/api/uploads/{upload_id}', { params: { path: { upload_id: uploadId! } } })),
    enabled: uploadId !== null,
    refetchInterval: (query) => (isJobWorking(query.state.data) ? POLL_MS : false),
  })
}

export function useCreateUpload() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Schemas['UploadCreateRequest']) => unwrap(api.POST('/api/uploads', { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['uploads'] }),
  })
}

export function useCancelUpload() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (uploadId: number) =>
      unwrap(api.POST('/api/uploads/{upload_id}/cancel', { params: { path: { upload_id: uploadId } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['uploads'] }),
  })
}

// Un upload annullato riparte da dove si era fermato.
export function useResumeUpload() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (uploadId: number) =>
      unwrap(api.POST('/api/uploads/{upload_id}/resume', { params: { path: { upload_id: uploadId } } })),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', job.id], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
  })
}

export function useDeleteUpload() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (uploadId: number) =>
      unwrap(api.DELETE('/api/uploads/{upload_id}', { params: { path: { upload_id: uploadId } } })),
    // Il dettaglio del job eliminato va tolto, non ricaricato: un refetch
    // darebbe 404 e smonterebbe la pagina prima della navigazione.
    onSuccess: (_, uploadId) => {
      queryClient.removeQueries({ queryKey: ['uploads', uploadId] })
      return queryClient.invalidateQueries({ queryKey: ['uploads'], exact: true })
    },
  })
}

export function useUploadTrackers() {
  return useQuery({
    queryKey: ['uploads', 'trackers'],
    queryFn: () => unwrap(api.GET('/api/uploads/trackers')),
  })
}

// Per l'avviso sugli host di immagini prima di un upload.
export function useImageHostStatus() {
  return useQuery({
    queryKey: ['uploads', 'image-hosts'],
    queryFn: () => unwrap(api.GET('/api/uploads/image-hosts')),
  })
}

export type EpisodeOrders = Schemas['EpisodeOrdersResponse']
export type EpisodeOrder = Schemas['EpisodeOrderResponse']

// Gli ordinamenti degli episodi di una serie candidata (nazgarr/library/episode_orders.py):
// proposta, quanto ci combaciano i file, avviso, episodi tradotti.
export function useEpisodeOrders(uploadId: number, tmdbId: number | null) {
  return useQuery({
    queryKey: ['uploads', uploadId, 'episode-orders', tmdbId],
    queryFn: () =>
      unwrap(
        api.GET('/api/uploads/{upload_id}/episode-orders', {
          params: { path: { upload_id: uploadId }, query: { tmdb_id: tmdbId! } },
        }),
      ),
    enabled: tmdbId != null,
    staleTime: 10 * 60 * 1000,
  })
}

export function useConfirmMatch(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Schemas['UploadMatchRequest']) =>
      unwrap(api.POST('/api/uploads/{upload_id}/match', { params: { path: { upload_id: uploadId } }, body })),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', uploadId], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
  })
}

// "Dividi in episodi" al match (nazgarr/upload/split.py): conferma il match del
// pack e lo divide in un upload per episodio.
export function useSplitUpload(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: Schemas['UploadMatchRequest']) =>
      unwrap(api.POST('/api/uploads/{upload_id}/split', { params: { path: { upload_id: uploadId } }, body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['uploads'] }),
  })
}

export function useReidentify(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (forcedIds: Schemas['ForcedIds']) =>
      unwrap(
        api.POST('/api/uploads/{upload_id}/reidentify', {
          params: { path: { upload_id: uploadId } },
          body: { forced_ids: forcedIds },
        }),
      ),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', uploadId], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
  })
}

// Dalla decisione torna al match (un match automatico sbagliato).
export function useRematch(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => unwrap(api.POST('/api/uploads/{upload_id}/rematch', { params: { path: { upload_id: uploadId } } })),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', uploadId], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
  })
}

export function useVerifyTarget(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ targetId, torrentIdRemote }: { targetId: number; torrentIdRemote: string }) =>
      unwrap(
        api.POST('/api/uploads/{upload_id}/targets/{target_id}/verify', {
          params: { path: { upload_id: uploadId, target_id: targetId } },
          body: { torrent_id_remote: torrentIdRemote },
        }),
      ),
    onSuccess: (job) => queryClient.setQueryData(['uploads', uploadId], job),
  })
}

// Di nuovo solo l'aggiunta al client di un upload pubblicato ma non in seed.
export function useRetrySeed(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (targetId: number) =>
      unwrap(
        api.POST('/api/uploads/{upload_id}/targets/{target_id}/retry-seed', {
          params: { path: { upload_id: uploadId, target_id: targetId } },
        }),
      ),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', uploadId], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
    // Anche un tentativo fallito aggiunge il suo evento al registro.
    onError: () => queryClient.invalidateQueries({ queryKey: ['uploads', uploadId] }),
  })
}

export function useUpdateOverrides(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (overrides: Record<string, unknown>) =>
      unwrap(
        api.PUT('/api/uploads/{upload_id}/overrides', {
          params: { path: { upload_id: uploadId } },
          body: { overrides },
        }),
      ),
    onSuccess: (job) => queryClient.setQueryData(['uploads', uploadId], job),
  })
}

export function useApproveUpload(uploadId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (targets: Schemas['TargetDecision'][]) =>
      unwrap(api.POST('/api/uploads/{upload_id}/approve', { params: { path: { upload_id: uploadId } }, body: { targets } })),
    onSuccess: (job) => {
      queryClient.setQueryData(['uploads', uploadId], job)
      queryClient.invalidateQueries({ queryKey: ['uploads'] })
    },
  })
}

export function useReorderQueue() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobIds: number[]) => unwrap(api.PUT('/api/uploads/queue', { body: { job_ids: jobIds } })),
    onSuccess: (jobs) => queryClient.setQueryData(['uploads'], jobs),
  })
}

// Il pattern dei nomi dei file nel torrent (Settings > Upload).
export function useFileNaming() {
  return useQuery({
    queryKey: ['uploads', 'file-naming'],
    queryFn: async () =>
      (await unwrap(api.GET('/api/uploads/file-naming'))) as unknown as { rules: NamingRulesShape; default: NamingRulesShape },
  })
}

type NamingRulesShape = Record<string, unknown>

export function useSaveFileNaming() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (rules: NamingRulesShape) => unwrap(api.PUT('/api/uploads/file-naming', { body: { rules } })),
    onSuccess: (data) => queryClient.setQueryData(['uploads', 'file-naming'], data),
  })
}

export function useFileNamingPreview(rules: NamingRulesShape | null) {
  return useQuery({
    queryKey: ['uploads', 'file-naming', 'preview', rules],
    queryFn: async () =>
      (await unwrap(api.POST('/api/uploads/file-naming/preview', { body: { rules: rules! } }))) as unknown as NamingPreview,
    enabled: rules !== null,
    placeholderData: (previous) => previous,
    retry: false,
  })
}
