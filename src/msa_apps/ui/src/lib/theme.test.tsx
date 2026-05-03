import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, useTheme } from './theme'

// Helper component that exposes theme state via rendered text
function ThemeDisplay() {
  const { theme, toggleTheme } = useTheme()
  return (
    <>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggleTheme}>toggle</button>
    </>
  )
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

describe('ThemeProvider', () => {
  it('defaults to dark when localStorage has no preference', () => {
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)
    expect(screen.getByTestId('theme').textContent).toBe('dark')
  })

  it('applies dark class to <html> on mount', () => {
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('restores light theme from localStorage', () => {
    localStorage.setItem('msa-theme', 'light')
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)
    expect(screen.getByTestId('theme').textContent).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('toggle switches dark → light and removes dark class', async () => {
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    await userEvent.click(screen.getByRole('button', { name: 'toggle' }))

    expect(screen.getByTestId('theme').textContent).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('toggle switches light → dark and adds dark class', async () => {
    localStorage.setItem('msa-theme', 'light')
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)

    await userEvent.click(screen.getByRole('button', { name: 'toggle' }))

    expect(screen.getByTestId('theme').textContent).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('persists theme choice to localStorage', async () => {
    render(<ThemeProvider><ThemeDisplay /></ThemeProvider>)
    await userEvent.click(screen.getByRole('button', { name: 'toggle' }))
    expect(localStorage.getItem('msa-theme')).toBe('light')
  })
})
