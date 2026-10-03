import { lazy, Suspense, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import { AppLayout } from '@/components/layout/AppLayout'
import { ComingSoon } from '@/pages/ComingSoon'
import { DashboardPage } from '@/pages/DashboardPage'
import { ConfigurationPage } from '@/pages/config/ConfigurationPage'
import { ReseedingPage } from '@/pages/reseeding/ReseedingPage'
import { FolderView } from '@/pages/library/FolderView'
import { LibraryDefaultView } from '@/pages/library/LibraryDefaultView'
import { PosterView } from '@/pages/library/PosterView'
import { NotImportedView } from '@/pages/torrent/NotImportedView'
import { TorrentFolderView } from '@/pages/torrent/TorrentFolderView'
import { NewUploadPage } from '@/pages/upload/NewUploadPage'
import { UploadJobPage } from '@/pages/upload/UploadJobPage'
import { UploadQueuePage } from '@/pages/upload/UploadQueuePage'
import { InstancesPage } from '@/pages/InstancesPage'
import { NAV_DASHBOARD, NAV_GROUPS } from '@/lib/nav'

// Ogni voce di navigazione (NAV_DASHBOARD + NAV_GROUPS) diventa una route:
// ComingSoon di default, sostituita da una pagina reale via `overrides`
// man mano che le sotto-fasi della Fase 8 la implementano (vedi il
// piano). Un solo posto dove aggiungere una nuova pagina reale, mai due
// elenchi di route da tenere sincronizzati a mano.
const overrides: Record<string, ReactNode> = {
  [NAV_DASHBOARD.to]: <DashboardPage />,
  '/library/poster': <PosterView />,
  '/library/folder': <FolderView />,
  '/torrent/folder': <TorrentFolderView />,
  '/torrent/triage': <NotImportedView />,
  '/reseeding': <ReseedingPage />,
  '/upload': <UploadQueuePage />,
  '/config': <ConfigurationPage />,
}

const RingLabPage = lazy(() => import('@/pages/lab/RingLabPage'))
const EyeLabPage = lazy(() => import('@/pages/lab/EyeLabPage'))

const ALL_ITEMS = [NAV_DASHBOARD, ...NAV_GROUPS.flatMap((group) => group.items)]

function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to={NAV_DASHBOARD.to} replace />} />
        <Route path="/library" element={<LibraryDefaultView />} />
        {/* "Non importati" è diventato Triage: i link salvati arrivano lì. */}
        <Route path="/torrent/not-imported" element={<Navigate to="/torrent/triage" replace />} />
        {/* Prototipo del logo (branch feature/ring-logo): fuori dalla navigazione,
            caricato a parte perché porta con sé Three.js. */}
        <Route
          path="/lab/ring"
          element={
            <Suspense fallback={<p className="text-sm text-muted-foreground">Loading…</p>}>
              <RingLabPage />
            </Suspense>
          }
        />
        {/* Prova dell'occhio della scansione (branch feat/fiery-eye). */}
        <Route
          path="/lab/eye"
          element={
            <Suspense fallback={<p className="text-sm text-muted-foreground">Loading…</p>}>
              <EyeLabPage />
            </Suspense>
          }
        />
        {ALL_ITEMS.map((item) => (
          <Route key={item.to} path={item.to} element={overrides[item.to] ?? <ComingSoon title={item.title} />} />
        ))}
        <Route path="/instances" element={<InstancesPage />} />
        <Route path="/upload/new" element={<NewUploadPage />} />
        <Route path="/upload/:uploadId" element={<UploadJobPage />} />
      </Route>
    </Routes>
  )
}

export default App
