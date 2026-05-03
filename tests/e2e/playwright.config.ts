import { defineConfig } from '@playwright/test'

const baseURL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8000'
const htmlReportDir = process.env.PLAYWRIGHT_HTML_REPORT ?? 'playwright-report'
const jsonReportPath = process.env.PLAYWRIGHT_JSON_REPORT ?? 'playwright-results.json'
const junitReportPath = process.env.PLAYWRIGHT_JUNIT_REPORT ?? 'playwright-junit.xml'
const resultsDir = process.env.PLAYWRIGHT_TEST_RESULTS_DIR ?? 'playwright-test-results'

export default defineConfig({
  testDir: './specs',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  outputDir: resultsDir,
  reporter: [
    ['html', { open: 'never', outputFolder: htmlReportDir }],
    ['json', { outputFile: jsonReportPath }],
    ['junit', { outputFile: junitReportPath }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
