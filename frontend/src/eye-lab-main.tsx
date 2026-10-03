// Anteprima autonoma dell'occhio della scansione (branch feat/fiery-eye):
// `npm run dev` e poi /eye-lab.html. Niente login né backend.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './index.css'
import EyeLabPage from '@/pages/lab/EyeLabPage'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <EyeLabPage />
  </StrictMode>,
)
