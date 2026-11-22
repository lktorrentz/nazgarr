import type { Schemas } from '@/api/client'
import { useFullChecks } from '@/api/hooks/fullChecks'
import { ErrorsPopover } from '@/components/ErrorsPopover'
import { StatusBadge } from '@/components/StateBadge'
import { Progress } from '@/components/ui/progress'
import { t } from '@/lib/i18n'
import { formatBytes } from '@/lib/library-filters'

type Review = Schemas['ReviewResponse']

// Esito del controllo completo partito da Approve (verify_before_execute):
// in corso con l'avanzamento, fallito con il motivo (la review resta qui).
export function VerifyStatus({ review }: { review: Review }) {
  const verifying = review.verify_status === 'verifying'
  const { data: checks } = useFullChecks({ poll: false })
  const check = verifying ? checks?.find((c) => c.id === review.verify_check_id) : undefined
  if (verifying) {
    const pct = check?.bytes_total ? (100 * check.bytes_done) / check.bytes_total : 0
    return (
      <div className="mt-1 grid max-w-sm gap-1">
        <Progress value={pct} />
        <span className="text-xs text-muted-foreground tabular-nums">
          {check?.bytes_total
            ? `${t('reseeding.verifying')} ${formatBytes(check.bytes_done)} / ${formatBytes(check.bytes_total)}`
            : t('reseeding.verifying')}
        </span>
      </div>
    )
  }
  if (review.verify_status === 'failed') {
    return (
      <div className="mt-1 flex items-center gap-1.5">
        <StatusBadge status="failed" compact>
          {t('reseeding.verifyFailed')}
        </StatusBadge>
        <ErrorsPopover count={1} messages={[review.verify_detail ?? '']} title={t('reseeding.verifyFailed')} />
      </div>
    )
  }
  return null
}
