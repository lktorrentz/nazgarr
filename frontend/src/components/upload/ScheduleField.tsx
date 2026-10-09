import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ToggleGroupItem, ToggleGroupSingle } from '@/components/ui/toggle-group'
import { t } from '@/lib/i18n'
import { defaultSchedule, localInputValue } from '@/lib/schedule'

// Quando parte un upload approvato: appena tocca a lui, o a un'ora scelta
// (nazgarr/upload/worker.py). value: il campo datetime-local, null = subito.
export function ScheduleField({ value, onChange }: { value: string | null; onChange: (value: string | null) => void }) {
  return (
    <div className="grid gap-2">
      <Label>{t('upload.schedule.when')}</Label>
      <ToggleGroupSingle
        value={value === null ? 'now' : 'later'}
        variant="outline"
        size="sm"
        className="w-fit gap-1.5"
        onValueChange={(choice) => choice && onChange(choice === 'now' ? null : defaultSchedule())}
      >
        <ToggleGroupItem value="now" className="rounded-md px-3">{t('upload.schedule.now')}</ToggleGroupItem>
        <ToggleGroupItem value="later" className="rounded-md px-3">{t('upload.schedule.later')}</ToggleGroupItem>
      </ToggleGroupSingle>
      {value !== null && (
        <div className="grid gap-1">
          <Input
            type="datetime-local"
            aria-label={t('upload.schedule.at')}
            className="w-fit"
            value={value}
            min={localInputValue(new Date())}
            onChange={(e) => onChange(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">{t('upload.schedule.help')}</p>
        </div>
      )}
    </div>
  )
}
