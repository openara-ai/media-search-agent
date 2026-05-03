import { test, expect } from '@playwright/test'

test('redirects root to search and renders the app shell', async ({ page }) => {
  await page.goto('/')

  await expect(page).toHaveURL(/\/search$/)
  await expect(page.getByRole('navigation')).toContainText('Media Search')
  await expect(page.getByPlaceholder("Describe what you're looking for…")).toBeVisible()
  await expect(page.getByRole('link', { name: 'Search' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Browse' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'People' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Indexer' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible()
})

test('navigates to the stable top-level pages', async ({ page }) => {
  await page.goto('/search')

  await page.getByRole('link', { name: 'Indexer' }).click()
  await expect(page).toHaveURL(/\/indexer$/)
  await expect(page.getByRole('heading', { name: 'Indexer' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Run Indexer|Run Again/ })).toBeVisible()

  await page.getByRole('link', { name: 'Settings' }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
  await expect(page.getByText('Media Sources', { exact: true })).toBeVisible()

  await page.getByRole('link', { name: 'People' }).click()
  await expect(page).toHaveURL(/\/people$/)
  await expect(page.getByRole('heading', { name: 'People' })).toBeVisible()
  await expect(page.getByText('Unknown faces')).toBeVisible()
})
