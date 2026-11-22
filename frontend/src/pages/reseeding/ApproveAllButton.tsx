import { Loader2Icon } from 'lucide-react'
import { useState } from 'react'

import { useApproveAll, useApproveAllStatus } from '@/api/hooks/reviews'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { t } from '@/lib/i18n'
import { cn } from '@/lib/utils'

// "Approva tutti" in cima alla coda (richiesta dell'utente, 2026-11-22): una
// conferma con i numeri letti all'apertura, di default solo le consigliate;
// quelle sotto soglia solo accendendo l'interruttore. Il giro gira sul
// server (nazgarr/reseed/approve_all.py), la notifica in basso lo segue.
export function ApproveAllButton() {
  const [open, setOpen] = useState(false)
  const [includeBelow, setIncludeBelow] = useState(false)
  const { data: status, isFetching } = useApproveAllStatus(open)
  const approveAll = useApproveAll()
  const count = status ? status.recommended + (includeBelow ? status.below_threshold : 0) : 0

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setIncludeBelow(false)
      }}
    >
      <DialogTrigger
        render={
          <Button size="sm" variant="outline" disabled={approveAll.isPending}>
            {approveAll.isPending && <Loader2Icon className="size-4 animate-spin" />}
            {t('reseeding.approveAll')}
          </Button>
        }
      />
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="pr-6">{t('reseeding.approveAllTitle')}</DialogTitle>
          <DialogDescription>{t('reseeding.approveAllDescription')}</DialogDescription>
        </DialogHeader>
        {status ? (
          <div className="grid gap-3 text-sm">
            <p className="font-medium">{t('reseeding.approveAllRecommended', { count: status.recommended })}</p>
            <label className="flex items-start gap-3">
              <Switch
                className="mt-0.5"
                checked={includeBelow}
                disabled={status.below_threshold === 0}
                onCheckedChange={(checked) => setIncludeBelow(checked)}
              />
              <span className="grid gap-0.5">
                <span>{t('reseeding.approveAllIncludeBelow', { count: status.below_threshold })}</span>
                <span className="text-xs text-muted-foreground">{t('reseeding.approveAllIncludeBelowHelp')}</span>
              </span>
            </label>
            {status.verify_failed > 0 && (
              <p className="text-xs text-muted-foreground">
                {t('reseeding.approveAllVerifyFailed', { count: status.verify_failed })}
              </p>
            )}
            <p className={cn('text-xs', status.verify_before_execute ? 'text-muted-foreground' : 'text-destructive')}>
              {t(status.verify_before_execute ? 'reseeding.approveAllWithVerify' : 'reseeding.approveAllWithoutVerify')}
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            {t('common.cancel')}
          </Button>
          {/* Mai con numeri vecchi: aspetta la lettura fresca dell'apertura. */}
          <Button
            disabled={!status || isFetching || count === 0 || approveAll.isPending}
            onClick={() => {
              approveAll.mutate(includeBelow)
              setOpen(false)
              setIncludeBelow(false)
            }}
          >
            {t('reseeding.approveAllConfirm', { count })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
