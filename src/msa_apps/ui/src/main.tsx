import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { ThemeProvider } from './lib/theme'
import './index.css'

// Apply saved theme before first paint to avoid flash
const savedTheme = localStorage.getItem('msa-theme') ?? 'dark'
document.documentElement.classList.toggle('dark', savedTheme === 'dark')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </React.StrictMode>,
)
