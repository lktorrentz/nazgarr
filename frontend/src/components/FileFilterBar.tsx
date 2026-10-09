import { EyeOffIcon, SearchIcon } from 'lucide-react'

import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Toggle } from '@/components/ui/toggle'
import { t } from '@/lib/i18n'
import { gigabyteLabel, type LibraryFilters, type StateSummary, type StatusOption } from '@/lib/library-filters'

export function FileFilterBar({
  statusOptions,
  summary,
  excludedCount,
  filters,
  onFiltersChange,
  actions,
}: {
  statusOptions: StatusOption[]
  summary: Record<string, StateSummary>
  excludedCount: number
  filters: LibraryFilters
  onFiltersChange: (filters: LibraryFilters) => void
  // A destra dei filtri per peso, es. "Componi un pack".
  actions?: React.ReactNode
}) {
  const set = <K extends keyof LibraryFilters>(key: K, value: LibraryFilters[K]) =>
    onFiltersChange({ ...filters, [key]: value })

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* Sotto sm le card di riepilogo sopra fanno già da filtro: le tab
            ripetevano le stesse scelte. Più strette dello schermo scorrono. */}
        <Tabs value={filters.status} onValueChange={(v) => set('status', v as string)} className="hidden max-w-full min-w-0 sm:flex">
          <TabsList className="scroll-strip max-w-full justify-start">
            {statusOptions.map((option) => (
              <TabsTrigger key={option.value} value={option.value}>
                {option.label} ({summary[option.value]?.count ?? 0})
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        {/* Gli esclusi sono fuori da ogni controllo: si vedono solo in "All",
            e solo a toggle premuto. */}
        <Toggle
          variant="outline"
          size="sm"
          pressed={filters.showExcluded}
          onPressedChange={(pressed) => set('showExcluded', pressed)}
          aria-label={t('library.showExcluded')}
        >
          <EyeOffIcon />
          {t('library.showExcluded')} {excludedCount > 0 && `(${excludedCount})`}
        </Toggle>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-56 flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={filters.search}
            onChange={(e) => set('search', e.target.value)}
            placeholder={t('library.searchPlaceholder')}
            className="pl-8"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <Input
            inputMode="decimal"
            value={filters.minGb}
            onChange={(e) => set('minGb', e.target.value)}
            placeholder={t('library.minSize', { unit: gigabyteLabel() })}
            aria-label={t('library.minSize', { unit: gigabyteLabel() })}
            className="w-24"
          />
          <span className="text-xs text-muted-foreground">–</span>
          <Input
            inputMode="decimal"
            value={filters.maxGb}
            onChange={(e) => set('maxGb', e.target.value)}
            placeholder={t('library.maxSize', { unit: gigabyteLabel() })}
            aria-label={t('library.maxSize', { unit: gigabyteLabel() })}
            className="w-24"
          />
        </div>
        {actions}
      </div>
    </div>
  )
}
