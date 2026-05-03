import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { FaceStrip } from './FaceStrip'
import type { FaceOnMedia } from '../../api/types'

function face(overrides: Partial<FaceOnMedia> = {}): FaceOnMedia {
  return {
    face_id: 'f1',
    bbox: [0, 0, 0.2, 0.2],
    confidence: 0.9,
    person_id: 'p1',
    person_name: 'Alice',
    gender: null,
    age: null,
    ...overrides,
  }
}

function renderStrip(faces: FaceOnMedia[]) {
  return render(<MemoryRouter><FaceStrip faces={faces} /></MemoryRouter>)
}

describe('FaceStrip', () => {
  it('renders nothing when all faces are unlabeled', () => {
    const { container } = renderStrip([face({ person_id: null, person_name: null })])
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when faces array is empty', () => {
    const { container } = renderStrip([])
    expect(container.firstChild).toBeNull()
  })

  it('renders one thumbnail per unique labeled person', () => {
    renderStrip([
      face({ face_id: 'f1', person_id: 'p1', person_name: 'Alice' }),
      face({ face_id: 'f2', person_id: 'p2', person_name: 'Bob' }),
    ])
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })

  it('deduplicates multiple faces for the same person', () => {
    renderStrip([
      face({ face_id: 'f1', person_id: 'p1', confidence: 0.7 }),
      face({ face_id: 'f2', person_id: 'p1', confidence: 0.9 }),
      face({ face_id: 'f3', person_id: 'p1', confidence: 0.5 }),
    ])
    expect(screen.getAllByRole('img')).toHaveLength(1)
  })

  it('keeps the highest-confidence face when deduplicating', () => {
    renderStrip([
      face({ face_id: 'f-low',  person_id: 'p1', confidence: 0.4 }),
      face({ face_id: 'f-high', person_id: 'p1', confidence: 0.95 }),
      face({ face_id: 'f-mid',  person_id: 'p1', confidence: 0.6 }),
    ])
    const img = screen.getByRole('img') as HTMLImageElement
    expect(img.src).toContain('f-high')
  })

  it('deduplicates per person independently', () => {
    renderStrip([
      face({ face_id: 'a1', person_id: 'p1', person_name: 'Alice', confidence: 0.8 }),
      face({ face_id: 'a2', person_id: 'p1', person_name: 'Alice', confidence: 0.6 }),
      face({ face_id: 'b1', person_id: 'p2', person_name: 'Bob',   confidence: 0.9 }),
    ])
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })

  it('shows person name as button title', () => {
    renderStrip([face({ person_id: 'p1', person_name: 'Alice' })])
    expect(screen.getByTitle('Alice')).toBeInTheDocument()
  })

  it('filters out unlabeled faces and renders only labeled ones', () => {
    renderStrip([
      face({ face_id: 'f1', person_id: 'p1', person_name: 'Alice' }),
      face({ face_id: 'f2', person_id: null, person_name: null }),
    ])
    expect(screen.getAllByRole('img')).toHaveLength(1)
  })
})
