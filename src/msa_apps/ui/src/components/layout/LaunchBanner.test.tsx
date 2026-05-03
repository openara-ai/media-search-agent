import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LaunchBanner } from './LaunchBanner'

function renderBanner(visible = true, onDismiss = () => {}) {
  return render(<LaunchBanner visible={visible} onDismiss={onDismiss} />)
}

describe('LaunchBanner', () => {
  const originalTitle = document.title

  beforeEach(() => {
    document.title = 'Media Search Agent'
  })

  afterEach(() => {
    document.title = originalTitle
  })

  it('shows when visible is true', async () => {
    renderBanner()

    expect(await screen.findByText('Media Search Agent')).toBeInTheDocument()
    expect(screen.getByText(/discover your forgotten moments/i)).toBeInTheDocument()
    expect(screen.getByText(window.location.origin, { exact: false })).toBeInTheDocument()
    expect(document.title).toBe('Media Search Agent - Just Opened')
  })

  it('does not show when visible is false', () => {
    renderBanner(false)
    expect(screen.queryByText('Media Search Agent')).not.toBeInTheDocument()
  })

  it('can be dismissed manually', async () => {
    const user = userEvent.setup()
    const onDismiss = vi.fn()
    renderBanner(true, onDismiss)

    const dismissButton = await screen.findByRole('button', { name: /dismiss launch banner/i })
    await user.click(dismissButton)

    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('dismisses when the local address pill is clicked', async () => {
    const user = userEvent.setup()
    const onDismiss = vi.fn()
    renderBanner(true, onDismiss)

    const originButton = await screen.findByRole('button', { name: /dismiss launch splash from local address/i })
    await user.click(originButton)

    expect(onDismiss).toHaveBeenCalledOnce()
  })

  it('focuses the dismiss button when opened', async () => {
    renderBanner()

    const dismissButton = await screen.findByRole('button', { name: /dismiss launch banner/i })

    await waitFor(() => expect(dismissButton).toHaveFocus())
  })

  it('dismisses when escape is pressed', async () => {
    const user = userEvent.setup()
    const onDismiss = vi.fn()
    renderBanner(true, onDismiss)

    await screen.findByText('Media Search Agent')
    await user.keyboard('{Escape}')

    expect(onDismiss).toHaveBeenCalledOnce()
  })
})
