import { FieryEye } from '@/components/FieryEye'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

// Pagina di prova dell'occhio della scansione (route /lab/eye, fuori dalla
// navigazione, e /eye-lab.html in sviluppo): in grande, alla misura vera
// dentro un pannello di scansione finto, su sfondo scuro e chiaro.

function MockRunPanel({ light }: { light?: boolean }) {
  return (
    <div className={cn('w-80 rounded-lg border shadow-lg', light ? 'border-zinc-200 bg-white text-zinc-900' : 'bg-card')}>
      <div className="flex items-start gap-3 px-4 py-3 text-sm">
        <FieryEye className="-ml-1 h-5 w-8 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="flex items-center justify-between gap-2 font-medium">
            <span className="truncate">Scan #42 in progress</span>
            <span className={cn('text-xs tabular-nums', light ? 'text-zinc-500' : 'text-muted-foreground')}>1:24</span>
          </p>
          <p className={cn('text-xs', light ? 'text-zinc-500' : 'text-muted-foreground')}>Indexing torrent clients · 812 / 2 140</p>
        </div>
      </div>
    </div>
  )
}

export default function EyeLabPage() {
  return (
    <div className="grid max-w-5xl gap-6">
      <Card>
        <CardHeader>
          <CardTitle>Scan icon</CardTitle>
          <CardDescription>
            The fiery eye shown while a scan runs. With "reduce motion" on, it stays still.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-8">
          <div className="flex flex-wrap items-end gap-10">
            <FieryEye className="h-48 w-80" title="Fiery eye, large" />
            <FieryEye className="h-20 w-32" />
            <FieryEye className="h-10 w-16" />
            <FieryEye className="h-5 w-8" />
          </div>
          <div className="flex flex-wrap gap-6">
            <MockRunPanel />
            <div className="rounded-lg bg-zinc-100 p-4">
              <MockRunPanel light />
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
