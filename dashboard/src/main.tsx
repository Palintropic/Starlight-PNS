import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Login from './Login.tsx'

// 仅供本地视觉验收：生产构建中 import.meta.env.DEV 恒为 false，不能借此绕过登录。
const previewLogin = import.meta.env.DEV && new URLSearchParams(window.location.search).has('preview-login')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {previewLogin ? <Login onDone={() => undefined} /> : <App />}
  </StrictMode>,
)
