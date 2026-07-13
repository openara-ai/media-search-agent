import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StartupGate } from './StartupGate'

// The model-download phase is covered in ModelDownload.test.tsx; here we stub it so the gate's
// hand-off can be asserted without a QueryClient/WebSocket. It only renders in the 'models' phase.
vi.mock('./ModelDownload', () => ({
  ModelDownload: (props: { initialModels: unknown[] }) => (
    <div>MODELS PHASE ({props.initialModels.length} models)</div>
  ),
}))

// URL-aware fetch stub: /health drives the gate; once it reports `ready`, StartupGate checks
// /api/setup/status before revealing children. `setup` defaults to models-present (ready:true) so
// the ready path goes straight to children; tests exercising the model phase pass a not-ready setup.
function stubHealth(body: () => unknown, setup: () => unknown = () => ({ ready: true, models: [] })) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const payload = url.includes('/api/setup/status') ? setup() : body()
      return { ok: true, json: async () => payload } as unknown as Response
    }),
  )
}

describe('StartupGate', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders children once /health reports ready', async () => {
    stubHealth(() => ({ status: 'ready' }))
    render(
      <StartupGate pollIntervalMs={5}>
        <div>APP READY</div>
      </StartupGate>,
    )
    expect(await screen.findByText('APP READY')).toBeInTheDocument()
  })

  it('hands to the model-download phase (same splash) when the backend is ready but models are missing', async () => {
    // /health ready but /api/setup/status not-ready → the gate STAYS up and shows the in-splash
    // model download (not the app), passing the fetched model list through. Reveals nothing yet.
    stubHealth(
      () => ({ status: 'ready' }),
      () => ({
        ready: false,
        models: [
          { id: 'clip', label: 'CLIP', size_mb: 850, present: false, integrity_hint: '', source: 'huggingface.co/x' },
        ],
      }),
    )
    render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(await screen.findByText(/MODELS PHASE \(1 models\)/i)).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument() // app still gated behind the models
  })

  it('shows the branded provisioning stage, detail and pct from the responder', async () => {
    stubHealth(() => ({
      status: 'provisioning',
      stage: 'deps-torch',
      pct: 42,
      detail: 'Installing PyTorch (cpu)',
      log: '/Users/me/Library/Logs/MediaSearchAgent/provision-x.log',
    }))
    render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    expect(await screen.findByText(/Installing PyTorch \(cpu\)/i)).toBeInTheDocument()
    // Deps fill the first 60% of the one continuous bar, so the responder's 42% shows as 25%.
    expect(await screen.findByText('25%')).toBeInTheDocument()
    // Children stay hidden until ready.
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('shows the rolling file list, step label, and an always-animated activity bar while provisioning', async () => {
    // "Both": a byte-backed bar (width from pct) PLUS the installer-style rolling list of wheels uv
    // is fetching — newest active, finished ones above — while the detail line stays the stable
    // step label. The fill carries the msa-provisioning-bar stripe so it reads as "alive" even when
    // the width holds flat during the silent multi-GB transfer.
    stubHealth(() => ({
      status: 'provisioning',
      stage: 'deps-torch',
      pct: 34,
      detail: 'Installing PyTorch (cu128)',
      files: ['torch-2.6.0+cu128-cp312-cp312-win_amd64.whl', 'nvidia-cudnn-cu12-9.1.0.70.whl'],
    }))
    render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    expect(await screen.findByText('Installing PyTorch (cu128)')).toBeInTheDocument()
    // the actual wheels landing show as files (newest is the active one)
    expect(await screen.findByText('nvidia-cudnn-cu12-9.1.0.70.whl')).toBeInTheDocument()
    expect(screen.getByText('torch-2.6.0+cu128-cp312-cp312-win_amd64.whl')).toBeInTheDocument()
    const bar = await screen.findByTestId('provision-bar')
    expect(bar).toHaveClass('msa-provisioning-bar') // continuous activity stripe
    expect(bar).toHaveStyle({ width: '20%' }) // 34% of deps → 20% of the unified 0–100 bar
  })

  it('shows the error screen, relaunch guidance and the log location on a responder error', async () => {
    // A responder `status:'error'` is TERMINAL: the shim called status.fail(...) and is parked in
    // _hold_error_state_inline(), serving this same payload until the process relaunches. So the
    // screen guides a relaunch (progress resumes) rather than a re-poll Retry that would loop back
    // to the same held error, while keeping the detail and the log affordance for troubleshooting.
    stubHealth(() => ({
      status: 'error',
      detail: 'Installing PyTorch (cpu) failed (uv exit 1) — see the provisioning log',
      log: '/Users/me/Library/Logs/MediaSearchAgent/provision-x.log',
    }))
    render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(await screen.findByText(/Setup could not finish/i)).toBeInTheDocument()
    expect(screen.getByText(/uv exit 1/i)).toBeInTheDocument()
    expect(screen.getByText(/reopen Media Search Agent/i)).toBeInTheDocument()
    // "Open logs" affordance surfaces the log directory (webview can't open files directly).
    expect(screen.getByTestId('log-dir')).toHaveTextContent(
      '/Users/me/Library/Logs/MediaSearchAgent',
    )
  })

  it('a terminal responder error guides a relaunch (progress resumes), NOT a bare re-poll Retry', async () => {
    // Round-5 finding (id 3534641735): when /health serves the shim's held `status:'error'` (low
    // disk, a uv non-zero exit, a missing config template), the shim is parked in
    // _hold_error_state_inline() and keeps serving the SAME error until the app PROCESS relaunches.
    // A re-poll Retry only re-reads it — a no-op loop on the primary first-run failure path. The
    // error screen must instead guide the honest recovery: quit and reopen (the provisioning ledger
    // is resumable, so progress resumes). This MUST fail on pre-fix code, which rendered a bare
    // Retry (and no relaunch guidance) for a responder error.
    stubHealth(() => ({
      status: 'error',
      detail: 'Not enough free disk space to finish setup (need ~5 GB).',
      log: '/Users/me/Library/Logs/MediaSearchAgent/provision-x.log',
    }))
    render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(await screen.findByText(/Setup could not finish/i)).toBeInTheDocument()
    // Honest recovery: relaunch guidance (quit + reopen; progress is saved and resumes).
    expect(screen.getByText(/reopen Media Search Agent/i)).toBeInTheDocument()
    expect(screen.getByText(/progress is saved/i)).toBeInTheDocument()
    // No bare re-poll Retry — it would loop straight back to the SAME held error.
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
    // The log affordance stays for troubleshooting.
    expect(screen.getByTestId('log-dir')).toBeInTheDocument()
    // Children never mount while the backend never reports ready.
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('goes to the error screen after the connection-refused budget elapses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('connection refused')
      }),
    )
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={20}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(
      await screen.findByText(/Could not reach the Media Search Agent backend/i, undefined, {
        timeout: 3000,
      }),
    ).toBeInTheDocument()
  })

  it('does not get stuck on "starting" for a persistent non-ok (500); eventually errors', async () => {
    // A reachable-but-unhealthy backend (persistent 500) must NOT be masked as "starting"
    // forever: a non-ok response is not a healthy answer, so it can't reset the refused/timeout
    // budget. Once connectTimeoutMs elapses the gate surfaces the error screen instead.
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () => ({ ok: false, status: 500, json: async () => ({}) }) as unknown as Response,
      ),
    )
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={20}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(
      await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    // Children never mount while the backend never reports ready.
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('surfaces a persistent non-ok (500) fast even AFTER provisioning was observed — no 10-min wait', async () => {
    // Regression guard for the sawProvisioningRef scoping (round-6 review, Claude + Codex P2):
    // provStalledMs must cover ONLY a genuinely unreachable /health (fetch reject), NOT a
    // reachable-but-non-ok response. Once provisioning has been observed, a persistent 5xx during the
    // provisioning→starting handoff must still surface on the aggressive connectTimeoutMs (~90 s in
    // prod), not wait out the 10-min provStalledMs budget. MUST fail on pre-fix code: there,
    // sawProvisioningRef routed the 500 through provStalledMs (10 s here), so the error screen would
    // not appear within this test's window.
    let calls = 0
    const fetchMock = vi.fn(async () => {
      calls += 1
      if (calls <= 2) {
        return {
          ok: true,
          json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: 10 }),
        } as unknown as Response
      }
      // Reachable but erroring after provisioning: the backend ANSWERS 500, it is not unreachable.
      return { ok: false, status: 500, json: async () => ({}) } as unknown as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={20} provStalledMs={10_000}>
        <div>THE APP</div>
      </StartupGate>,
    )
    // Provisioning shows first (calls 1-2, sets the sticky sawProvisioning ref)...
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    // ...then the persistent 500 must trip the SHORT connectTimeoutMs (20 ms), not provStalledMs
    // (10 s): the error screen appears well inside the window a pre-fix build would have waited out.
    expect(
      await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('does not wait forever on a backend pinned at 200 {status:"starting"} — eventually errors', async () => {
    // Round-2 finding: FastAPI's _lifespan can swallow a startup exception and leave /health pinned
    // at 200 {status:"starting"}. res.ok is true, so this is NOT a refused/non-ok case — round-1's
    // budget never charges. Without a no-progress bound the UI hangs on "Starting…" forever (this
    // test times out → fails on pre-fix code). With the bound, a reachable response that never shows
    // forward progress spends progressTimeoutMs and surfaces the error/Retry screen.
    stubHealth(() => ({ status: 'starting' }))
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={5000} progressTimeoutMs={20}>
        <div>THE APP</div>
      </StartupGate>,
    )
    expect(
      await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    // A wedged `starting` keeps the (cheap) re-poll Retry in case the backend is merely slow...
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    // ...but it can't self-heal either, so it also surfaces the relaunch guidance as the real fix.
    expect(screen.getByText(/reopen Media Search Agent/i)).toBeInTheDocument()
    // Children never mount while the backend never reports ready.
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('does NOT error while provisioning progress keeps advancing (real multi-minute install)', async () => {
    // Regression guard: a genuine first-run install reports "provisioning" for minutes. The
    // progressTimeoutMs budget is gated to the FastAPI `starting` phase and never applies to
    // `provisioning`, so the gate keeps showing progress and never trips the error screen inside the
    // window — even with a tiny progressTimeoutMs that a wedged `starting` backend blows through in
    // ~4 polls. (Advancing pct is the easy case; the flat-pct case below is the round-4 fix.)
    let pct = 0
    const fetchMock = vi.fn(
      async () =>
        ({
          ok: true,
          json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: (pct += 5) }),
        }) as unknown as Response,
    )
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate pollIntervalMs={5} progressTimeoutMs={20}>
        <div>THE APP</div>
      </StartupGate>,
    )
    // It renders provisioning progress...
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    // ...and keeps polling well past progressTimeoutMs (~12 advancing polls; a *stalled* backend
    // would have errored by poll ~5) — it must still be waiting, never the error screen.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(12), { timeout: 3000 })
    expect(screen.queryByText(/Setup could not finish/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Installing ML libraries/i)).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('does NOT error on a flat provisioning stage:pct held past progressTimeoutMs (piped-uv silence)', async () => {
    // Round-4 finding (the false-trip fix): uv suppresses its progress bars when stdout is piped
    // (our case), so during a real multi-minute torch install `_run_step` emits the SAME {stage,pct}
    // for far longer than progressTimeoutMs. That flat pct is NORMAL, not a stall — the gate must
    // keep waiting on 'provisioning' and never surface the error screen. This MUST fail on pre-fix
    // code: there, a flat provisioning payload spends the no-progress budget → error screen by
    // poll ~5, which both assertions below reject.
    const fetchMock = vi.fn(
      async () =>
        ({
          ok: true,
          json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: 10 }),
        }) as unknown as Response,
    )
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate pollIntervalMs={5} progressTimeoutMs={20}>
        <div>THE APP</div>
      </StartupGate>,
    )
    // It renders provisioning progress on the flat pct...
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    // ...and keeps polling well past progressTimeoutMs on the SAME stage:pct — a pre-fix build would
    // have flipped to the error screen by poll ~5; the fixed build stays on progress. (>= keeps the
    // assertion robust to poll-cadence jitter, so looping this file never flakes.)
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(12), {
      timeout: 3000,
    })
    expect(screen.queryByText(/Setup could not finish/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Installing ML libraries/i)).toBeInTheDocument()
    // responder pct 10 → 6% on the unified bar (deps span 0–60%)
    expect(screen.getByText('6%')).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('does NOT error on an unreachable /health once provisioning has been observed (NVIDIA cold-install blip)', async () => {
    // Real-hardware finding: on a cold first run the ~2 GB cu128 torch download holds `deps-torch` for
    // minutes (field logs: 4m17s), during which the responder can stop answering /health. Pre-fix, that
    // unreachable window spent the 90 s connect budget and flipped to the error screen mid-install — even
    // though provisioning was progressing fine (it completed in the logs; a warm relaunch with instant
    // provisioning never hit the window). Once `provisioning` has been observed, an unreachable /health
    // must fall back to the far larger provStalledMs budget, NOT connectTimeoutMs. This is the class of
    // bug the CPU-only desktop BVT can't reproduce (its non-NVIDIA torch install is seconds, so the
    // window never opens). MUST fail on pre-fix code: there, connectTimeoutMs fires a few polls into the
    // unreachable stretch and both no-error assertions below reject.
    let calls = 0
    const fetchMock = vi.fn(async () => {
      calls += 1
      if (calls <= 2) {
        return {
          ok: true,
          json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: 10 }),
        } as unknown as Response
      }
      throw new TypeError('Failed to fetch') // responder unreachable during the long cold install
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={20} provStalledMs={10_000}>
        <div>THE APP</div>
      </StartupGate>,
    )
    // Provisioning shows first (calls 1-2)...
    expect(await screen.findByText(/Installing ML libraries/i)).toBeInTheDocument()
    // ...then /health is unreachable for many polls — well past connectTimeoutMs (20 ms) but far inside
    // provStalledMs (10 s). A pre-fix build flips to the error screen by poll ~7; the fixed build holds
    // the last-good provisioning screen because provisioning was observed.
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(20), { timeout: 3000 })
    expect(screen.queryByText(/Setup could not finish/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Installing ML libraries/i)).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('a sidecar that dies unreachable AFTER provisioning completes fails fast (not the 10-min budget)', async () => {
    // Round-6 follow-up (Codex P2): the sticky provisioning flag is DROPPED once a `starting` 200 is
    // seen, so a subsequently-unreachable /health (sidecar crashed during/after the responder→uvicorn
    // handoff) reverts to the aggressive connectTimeoutMs instead of the 10-min provStalledMs.
    // Sequence: provisioning (sets sticky) → starting 200 (clears it) → fetch rejects forever. MUST
    // fail on the intermediate build where the flag stayed sticky-forever (the reject would sit ~10 min).
    let calls = 0
    const fetchMock = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return {
          ok: true,
          json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: 90 }),
        } as unknown as Response
      }
      if (calls === 2) {
        return { ok: true, json: async () => ({ status: 'starting' }) } as unknown as Response
      }
      throw new TypeError('Failed to fetch') // sidecar unreachable after the provisioning handoff
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate
        pollIntervalMs={5}
        connectTimeoutMs={20}
        progressTimeoutMs={5000}
        provStalledMs={10_000}
      >
        <div>THE APP</div>
      </StartupGate>,
    )
    // Errors on connectTimeoutMs (20 ms), well inside the 10 s provStalledMs a sticky-forever flag
    // would have imposed — provisioning ended (starting seen), so the blip budget no longer applies.
    expect(
      await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('Retry after a non-ok during provisioning does not re-arm the 10-min blip budget', async () => {
    // Round-6 third follow-up (Codex P2): a reachable non-ok (`!res.ok`) also clears the sticky
    // provisioning flag. Otherwise, after a 5xx-during-provisioning error, clicking Retry and then
    // hitting a fetch-reject would wait the 10-min provStalledMs off the stale ref instead of the 90 s
    // connect budget. Sequence: provisioning → persistent 500 (errors on connectTimeoutMs) → Retry →
    // fetch rejects forever → must re-error fast. MUST fail on the round-2 build (flag only cleared on
    // a `starting` 200, so the post-Retry reject would sit ~10 min).
    let mode = 'prov' // 'prov' = provisioning then 500; Retry flips it to 'reject'
    let calls = 0
    const fetchMock = vi.fn(async () => {
      calls += 1
      if (mode === 'prov') {
        if (calls === 1) {
          return {
            ok: true,
            json: async () => ({ status: 'provisioning', stage: 'deps-torch', pct: 50 }),
          } as unknown as Response
        }
        return { ok: false, status: 500, json: async () => ({}) } as unknown as Response
      }
      throw new TypeError('Failed to fetch') // responder degraded to unreachable after the 5xx
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={20} provStalledMs={10_000}>
        <div>THE APP</div>
      </StartupGate>,
    )
    // First error from the persistent 500 (charged on connectTimeoutMs)...
    await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 })
    // ...now the responder degrades to fetch-reject; Retry must NOT wait the 10-min blip budget,
    // because the earlier reachable 500 already dropped the sticky provisioning flag.
    mode = 'reject'
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(
      await screen.findByText(/Setup could not finish/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
  })

  it('does not update state after unmount while a /health fetch is in flight', async () => {
    // Guard the post-await cancelled check: unmounting mid-request must not trigger a state
    // update on the torn-down component. React 18 makes such an update a silent no-op, but one
    // escaping act() surfaces as a console error — assert none does.
    let resolveFetch: ((v: unknown) => void) | undefined
    const pending = new Promise((resolve) => {
      resolveFetch = resolve
    })
    vi.stubGlobal('fetch', vi.fn(() => pending))
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { unmount } = render(
      <StartupGate pollIntervalMs={5}>
        <div>THE APP</div>
      </StartupGate>,
    )
    unmount() // tear down while the first /health request is still pending
    // Now let the in-flight fetch resolve — the post-await guard must prevent any setState.
    resolveFetch?.({ ok: true, json: async () => ({ status: 'ready' }) } as unknown as Response)
    await new Promise((r) => setTimeout(r, 0))

    expect(errorSpy.mock.calls.some((c) => String(c[0]).includes('not wrapped in act'))).toBe(false)
    expect(screen.queryByText('THE APP')).not.toBeInTheDocument()
    errorSpy.mockRestore()
  })

  it('retry re-polls and recovers to ready on a transient (wedged-starting) error', async () => {
    // The re-poll Retry is for the TRANSIENT paths only (a terminal responder error offers relaunch
    // guidance instead — covered above). Here a backend wedged at {status:'starting'} trips the
    // no-progress budget → error screen with Retry; once the backend flips to ready, clicking Retry
    // re-polls and recovers.
    let ready = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        // Models present, so a `ready` /health goes straight to children (no model phase here).
        if (String(input).includes('/api/setup/status')) {
          return { ok: true, json: async () => ({ ready: true, models: [] }) } as unknown as Response
        }
        return {
          ok: true,
          json: async () => (ready ? { status: 'ready' } : { status: 'starting' }),
        } as unknown as Response
      }),
    )
    render(
      <StartupGate pollIntervalMs={5} connectTimeoutMs={5000} progressTimeoutMs={20}>
        <div>RECOVERED</div>
      </StartupGate>,
    )
    await screen.findByText(/Setup could not finish/i)
    ready = true
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByText('RECOVERED')).toBeInTheDocument()
  })
})
