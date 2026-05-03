import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MediaCard } from './MediaCard'

// Mock react-router-dom navigate (used by FaceStrip internally)
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))

const baseProps = {
  id: 'abc123',
  onClick: vi.fn(),
}

describe('MediaCard', () => {
  describe('thumbnail', () => {
    it('renders an img with src derived from media_id (not path)', () => {
      const { container } = render(<MediaCard {...baseProps} path="/mnt/p/Photos/IMG_001.heic" />)
      const img = container.querySelector('img')
      expect(img).not.toBeNull()
      // Thumbnail is keyed by id (media_id), not the file path — avoids stem collisions
      expect(img).toHaveAttribute('src', '/thumbnails/abc123.jpg')
    })

    it('renders no img when id is null', () => {
      const { container } = render(<MediaCard {...baseProps} id={null} />)
      expect(container.querySelector('img')).toBeNull()
    })

    it('renders no img when id is empty string', () => {
      const { container } = render(<MediaCard {...baseProps} id="" />)
      expect(container.querySelector('img')).toBeNull()
    })
  })

  describe('score badge', () => {
    it('shows score badge when score is provided', () => {
      render(<MediaCard {...baseProps} score={0.73} />)
      expect(screen.getByText('73%')).toBeInTheDocument()
    })

    it('rounds score to nearest integer', () => {
      render(<MediaCard {...baseProps} score={0.856} />)
      expect(screen.getByText('86%')).toBeInTheDocument()
    })

    it('does not show score badge when score is null', () => {
      render(<MediaCard {...baseProps} score={null} />)
      expect(screen.queryByText(/%/)).toBeNull()
    })

    it('does not show score badge when score is undefined', () => {
      render(<MediaCard {...baseProps} />)
      expect(screen.queryByText(/%/)).toBeNull()
    })
  })

  describe('video badge', () => {
    it('shows video badge for video type', () => {
      render(<MediaCard {...baseProps} type="video" />)
      // Badge shows "video" text when no duration
      expect(screen.getByText('video')).toBeInTheDocument()
    })

    it('shows formatted duration when video has duration', () => {
      render(<MediaCard {...baseProps} type="video" duration={125} />)
      expect(screen.getByText('2:05')).toBeInTheDocument()
    })

    it('does not show video badge for image type', () => {
      render(<MediaCard {...baseProps} type="image" />)
      expect(screen.queryByText('video')).toBeNull()
    })
  })

  describe('interaction', () => {
    it('calls onClick when clicked', async () => {
      const onClick = vi.fn()
      render(<MediaCard {...baseProps} onClick={onClick} />)
      await userEvent.click(screen.getByRole('button'))
      expect(onClick).toHaveBeenCalledOnce()
    })
  })
})
