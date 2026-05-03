import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FilterPanel } from './FilterPanel'

const baseProps = {
  filters: {},
  onChange: vi.fn(),
  minScore: 15,
  onMinScoreChange: vi.fn(),
}

describe('FilterPanel', () => {
  describe('min score control', () => {
    it('renders the slider', () => {
      render(<FilterPanel {...baseProps} />)
      expect(screen.getByRole('slider')).toBeInTheDocument()
    })

    it('displays current minScore as percentage', () => {
      render(<FilterPanel {...baseProps} minScore={45} />)
      expect(screen.getByText('45%')).toBeInTheDocument()
    })

    it('calls onMinScoreChange when slider moves', () => {
      const onMinScoreChange = vi.fn()
      render(<FilterPanel {...baseProps} onMinScoreChange={onMinScoreChange} />)
      fireEvent.change(screen.getByRole('slider'), { target: { value: '40' } })
      expect(onMinScoreChange).toHaveBeenCalledWith(40)
    })

    it('increment button increases minScore by 1', async () => {
      const onMinScoreChange = vi.fn()
      render(<FilterPanel {...baseProps} minScore={30} onMinScoreChange={onMinScoreChange} />)
      await userEvent.click(screen.getByRole('button', { name: /increase min score/i }))
      expect(onMinScoreChange).toHaveBeenCalledWith(31)
    })

    it('decrement button decreases minScore by 1', async () => {
      const onMinScoreChange = vi.fn()
      render(<FilterPanel {...baseProps} minScore={30} onMinScoreChange={onMinScoreChange} />)
      await userEvent.click(screen.getByRole('button', { name: /decrease min score/i }))
      expect(onMinScoreChange).toHaveBeenCalledWith(29)
    })

    it('decrement button is disabled at 0', () => {
      render(<FilterPanel {...baseProps} minScore={0} />)
      expect(screen.getByRole('button', { name: /decrease min score/i })).toBeDisabled()
    })

    it('increment button is disabled at 100', () => {
      render(<FilterPanel {...baseProps} minScore={100} />)
      expect(screen.getByRole('button', { name: /increase min score/i })).toBeDisabled()
    })
  })

  describe('media type toggles', () => {
    it('renders Photos and Videos toggles', () => {
      render(<FilterPanel {...baseProps} />)
      expect(screen.getByText('Photos')).toBeInTheDocument()
      expect(screen.getByText('Videos')).toBeInTheDocument()
    })

    it('calls onChange with image type when Photos clicked', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} onChange={onChange} />)
      await userEvent.click(screen.getByText('Photos'))
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ media_type: 'image' }))
    })

    it('calls onChange with null when active type is clicked again (deselect)', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} filters={{ media_type: 'image' }} onChange={onChange} />)
      await userEvent.click(screen.getByText('Photos'))
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ media_type: null }))
    })
  })

  describe('date range presets', () => {
    it('renders the date preset dropdown', () => {
      render(<FilterPanel {...baseProps} />)
      expect(screen.getByRole('combobox')).toBeInTheDocument()
    })

    it('defaults to "Any time"', () => {
      render(<FilterPanel {...baseProps} />)
      expect(screen.getByRole('combobox')).toHaveValue('')
    })

    it('shows date inputs only when Custom is selected', async () => {
      render(<FilterPanel {...baseProps} />)
      expect(screen.queryByDisplayValue('')).not.toHaveAttribute('type', 'date')
      await userEvent.selectOptions(screen.getByRole('combobox'), 'custom')
      expect(screen.getAllByDisplayValue('').some(el => el.getAttribute('type') === 'date')).toBe(true)
    })

    it('calls onChange with date_from and date_to when Past week selected', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} onChange={onChange} />)
      await userEvent.selectOptions(screen.getByRole('combobox'), 'week')
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
        date_from: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
        date_to: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      }))
    })

    it('calls onChange with date_from and date_to when Past month selected', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} onChange={onChange} />)
      await userEvent.selectOptions(screen.getByRole('combobox'), 'month')
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
        date_from: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
        date_to: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      }))
    })

    it('past week date_from is 7 days before date_to', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} onChange={onChange} />)
      await userEvent.selectOptions(screen.getByRole('combobox'), 'week')
      const call = onChange.mock.calls[0][0]
      const diff = (new Date(call.date_to).getTime() - new Date(call.date_from).getTime()) / 86_400_000
      expect(diff).toBe(7)
    })
  })

  describe('clear button', () => {
    it('does not show clear button when nothing is set and minScore is default', () => {
      render(<FilterPanel {...baseProps} minScore={15} filters={{}} />)
      expect(screen.queryByText('Clear filters')).toBeNull()
    })

    it('shows clear button when a filter is active', () => {
      render(<FilterPanel {...baseProps} filters={{ media_type: 'image' }} />)
      expect(screen.getByText('Clear filters')).toBeInTheDocument()
    })

    it('shows clear button when minScore differs from default (15)', () => {
      render(<FilterPanel {...baseProps} minScore={30} />)
      expect(screen.getByText('Clear filters')).toBeInTheDocument()
    })

    it('resets filters, minScore to 15, and date preset on clear', async () => {
      const onChange = vi.fn()
      const onMinScoreChange = vi.fn()
      render(
        <FilterPanel
          filters={{ media_type: 'image' }}
          onChange={onChange}
          minScore={40}
          onMinScoreChange={onMinScoreChange}
        />
      )
      await userEvent.click(screen.getByText('Clear filters'))
      expect(onChange).toHaveBeenCalledWith({})
      expect(onMinScoreChange).toHaveBeenCalledWith(15)
    })

    it('resets date preset dropdown to "Any time" on clear', async () => {
      const onChange = vi.fn()
      render(<FilterPanel {...baseProps} filters={{ media_type: 'image' }} onChange={onChange} />)
      // First select a preset
      await userEvent.selectOptions(screen.getByRole('combobox'), 'month')
      expect(screen.getByRole('combobox')).toHaveValue('month')
      // Then clear
      await userEvent.click(screen.getByText('Clear filters'))
      expect(screen.getByRole('combobox')).toHaveValue('')
    })
  })
})
