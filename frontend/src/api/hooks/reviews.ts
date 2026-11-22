import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type Schemas, unwrap } from '@/api/client'
import { type Activity, pushActivity, updateActivity } from '@/lib/activity'
import { trackVerification } from '@/lib/verification'
import { t } from '@/lib/i18n'

// Review per pagina della coda: il server chiude ogni pagina alla fine di un
// gruppo (gli episodi di una stagione), quindi può darne qualcuna in più.
export const REVIEW_QUEUE_PAGE = 100

// La coda a pagine (audit 2026-10-18: prima sempre tutta, con il server che la
// ricalcolava intera dopo ogni decisione). Dopo un'approvazione TanStack
// ricarica le pagine già lette una dopo l'altra, ognuna da dove finisce la
// precedente appena ricaricata: niente righe doppie o saltate.
export function useReviewQueue() {
  return useInfiniteQuery({
    queryKey: ['reviews', 'queue'],
    queryFn: ({ pageParam }) =>
      unwrap(api.GET('/api/reviews/queue', { params: { query: { offset: pageParam, limit: REVIEW_QUEUE_PAGE } } })),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
}

export function useApproveReview() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => unwrap(api.POST('/api/reviews/{review_id}/approve', { params: { path: { review_id: id } } })),
    // Feedback in basso a destra: l'approvazione crea hardlink e aggiunge il
    // torrent al client, può durare qualche secondo; poi il recheck lo segue
    // SeedJobWatcher, che avvisa quando finisce.
    onMutate: () => ({ activityId: pushActivity({ status: 'running', title: t('activity.approving') }) }),
    onError: (error, _id, context) => {
      if (context) updateActivity(context.activityId, { status: 'error', title: t('activity.approveFailed'), detail: error.message })
    },
    onSuccess: (review, _id, context) => {
      if (context && review.verify_status === 'verifying' && review.verify_check_id) {
        // Prima il controllo completo: la notifica la aggiorna il watcher in
        // ActivityStack fino all'esito (ed eventuale aggiunta al client).
        updateActivity(context.activityId, { title: t('activity.verifyQueued'), detail: review.candidate_name, progress: 0 })
        trackVerification(review.verify_check_id, context.activityId)
      } else if (context) {
        const job = review.seed_job
        updateActivity(context.activityId, job?.final_status === 'failed'
          ? { status: 'error', title: t('activity.executionFailed'), detail: job.error_message ?? review.candidate_name }
          : job
            ? { status: 'success', title: t('activity.addedRecheckPending'), detail: review.candidate_name }
            : { status: 'info', title: t('activity.approvedNoClient'), detail: review.candidate_name })
      }
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['reviews', 'failed'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
    },
  })
}

// Più review insieme (gli episodi singoli di una stagione): una alla volta,
// come dal bottone di ognuna, con una notifica sola. Con la verifica accesa
// ogni approvazione mette in coda il suo controllo, che si vede sulla riga.
export function useDecideReviews() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ ids, action }: { ids: number[]; action: 'approve' | 'reject' }) => {
      const errors: string[] = []
      for (const id of ids) {
        const params = { params: { path: { review_id: id } } }
        try {
          if (action === 'approve') await unwrap(api.POST('/api/reviews/{review_id}/approve', params))
          else await unwrap(api.POST('/api/reviews/{review_id}/reject', params))
        } catch (error) {
          errors.push(error instanceof Error ? error.message : String(error))
        }
      }
      return { done: ids.length - errors.length, errors }
    },
    onMutate: ({ ids, action }) => ({
      activityId: pushActivity({
        status: 'running',
        title: t(action === 'approve' ? 'activity.approvingMany' : 'activity.rejectingMany', { count: ids.length }),
      }),
    }),
    onSettled: (result, error, { action }, context) => {
      if (context) {
        const done = t(action === 'approve' ? 'activity.approvedMany' : 'activity.rejectedMany', { count: result?.done ?? 0 })
        const failed = error ? [error.message] : (result?.errors ?? [])
        updateActivity(context.activityId, failed.length > 0
          ? { status: 'error', title: `${done} · ${t('activity.someFailed', { count: failed.length })}`, detail: failed[0] }
          : { status: 'success', title: done })
      }
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
    },
  })
}

export function useRejectReview() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => unwrap(api.POST('/api/reviews/{review_id}/reject', { params: { path: { review_id: id } } })),
    onError: (error) => {
      pushActivity({ status: 'error', title: t('activity.rejectFailed'), detail: error.message })
    },
    onSuccess: (review) => {
      pushActivity({ status: 'info', title: t('activity.rejected'), detail: review.candidate_name })
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
    },
  })
}

export function useFailedSeedJobs() {
  return useQuery({
    queryKey: ['reviews', 'failed'],
    queryFn: () => unwrap(api.GET('/api/reviews/failed')),
  })
}

export function useRetryFailed() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (seedJobId: number) =>
      unwrap(api.POST('/api/reviews/failed/{seed_job_id}/retry', { params: { path: { seed_job_id: seedJobId } } })),
    // ['reviews'] copre anche l'elenco delle esecuzioni (seed-jobs/recent).
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reviews'] }),
  })
}

export function useDeleteSeedJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (seedJobId: number) =>
      unwrap(api.DELETE('/api/reviews/seed-jobs/{seed_job_id}', { params: { path: { seed_job_id: seedJobId } } })),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
    },
  })
}

export function useCandidateAudit(mediaItemId: number | null) {
  return useQuery({
    queryKey: ['reviews', 'candidates', mediaItemId],
    queryFn: () =>
      unwrap(api.GET('/api/reviews/candidates/{media_item_id}', { params: { path: { media_item_id: mediaItemId! } } })),
    enabled: mediaItemId !== null,
  })
}

export function useReconcileNow() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => unwrap(api.POST('/api/reviews/seed-jobs/reconcile')),
    onMutate: () => ({ activityId: pushActivity({ status: 'running', title: t('activity.checkingRechecks') }) }),
    onError: (error, _v, context) => {
      if (context) updateActivity(context.activityId, { status: 'error', title: t('activity.checkFailed'), detail: error.message })
    },
    onSuccess: (result, _v, context) => {
      // L'esito dei singoli seed job lo annuncia SeedJobWatcher; qui solo il controllo.
      if (context) updateActivity(context.activityId, { status: 'info', title: t('activity.checked', { count: result.reconciled }) })
      queryClient.invalidateQueries({ queryKey: ['library'] })
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
    },
  })
}

// Ultime esecuzioni, per SeedJobWatcher: ogni 10s mentre qualcuna è in
// attesa del recheck, altrimenti ogni minuto (un seed può partire anche
// da un'altra scheda o da una run).
export function useRecentSeedJobs() {
  return useQuery({
    queryKey: ['reviews', 'seed-jobs', 'recent'],
    // Le stesse 200 righe servono al watcher dei recheck e all'elenco
    // delle esecuzioni in Reseeding.
    queryFn: () => unwrap(api.GET('/api/reviews/seed-jobs/recent', { params: { query: { limit: 200 } } })),
    refetchInterval: (query) =>
      query.state.data?.some((job) => job.final_status === 'in_progress') ? 10_000 : 60_000,
  })
}

// Categoria e tag nel client scelti a mano per un reseed, come per un
// upload: null torna ai default del client, "" vuol dire nessuno.
export function useSetReviewClientLabels(reviewId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { client_category: string | null; client_tags: string | null }) =>
      unwrap(api.PUT('/api/reviews/{review_id}/client-labels', { params: { path: { review_id: reviewId } }, body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reviews'] }),
  })
}

// "Approva tutti" in cima alla coda (2026-11-22): quante review prenderebbe
// adesso e l'ultimo giro. Letto all'apertura della conferma, sempre fresco:
// i numeri sono quelli che l'utente approva.
export function useApproveAllStatus(enabled: boolean) {
  return useQuery({
    queryKey: ['reviews', 'approve-all'],
    queryFn: () => unwrap(api.GET('/api/reviews/approve-all')),
    enabled,
    staleTime: 0,
  })
}

type ApproveAllBatch = Schemas['ApproveAllBatchResponse']

const APPROVE_ALL_POLL_MS = 1000

function approveAllOutcome(batch: ApproveAllBatch): Partial<Omit<Activity, 'id' | 'createdAt'>> {
  if (batch.total === 0) return { status: 'info', title: t('activity.nothingToApprove'), progress: null }
  const title = t(batch.verify ? 'activity.approvedAllVerify' : 'activity.approvedMany', { count: batch.approved })
  const notes = [
    batch.skipped > 0 ? t('activity.decidedMeanwhile', { count: batch.skipped }) : null,
    batch.failed > 0 ? t('activity.someFailed', { count: batch.failed }) : null,
  ].filter(Boolean)
  return {
    status: batch.failed > 0 ? 'error' : 'success',
    title: notes.length > 0 ? `${title} · ${notes.join(' · ')}` : title,
    detail: batch.last_error ?? undefined,
    progress: null,
  }
}

// Il giro gira sul server (con la verifica spenta crea hardlink e aggiunge
// torrent: minuti, con centinaia di review): qui lo si segue con una
// notifica fino alla fine. Chiusa la pagina, il giro continua lo stesso.
export function useApproveAll() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (includeBelowThreshold: boolean) => {
      const activityId = pushActivity({ status: 'running', title: t('activity.approvingAll') })
      try {
        let batch = await unwrap(
          api.POST('/api/reviews/approve-all', { body: { include_below_threshold: includeBelowThreshold } }),
        )
        // I controlli completi appena messi in coda: il watcher li segue.
        queryClient.invalidateQueries({ queryKey: ['full-checks', 'all'] })
        for (let polls = 1; batch.finished_at == null; polls++) {
          updateActivity(activityId, {
            title: t('activity.approvingMany', { count: batch.total }),
            detail: `${batch.done} / ${batch.total}`,
            progress: batch.total ? (100 * batch.done) / batch.total : 0,
          })
          await new Promise((resolve) => setTimeout(resolve, APPROVE_ALL_POLL_MS))
          const status = await unwrap(api.GET('/api/reviews/approve-all'))
          if (status.batch?.id !== batch.id) break // Nazgarr riavviato nel frattempo
          batch = status.batch
          if (polls % 5 === 0) queryClient.invalidateQueries({ queryKey: ['reviews', 'queue'] })
        }
        updateActivity(activityId, approveAllOutcome(batch))
        return batch
      } catch (error) {
        updateActivity(activityId, {
          status: 'error',
          title: t('activity.approveFailed'),
          detail: error instanceof Error ? error.message : String(error),
          progress: null,
        })
        throw error
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['reviews'] })
      queryClient.invalidateQueries({ queryKey: ['full-checks', 'all'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['library'] })
    },
  })
}
