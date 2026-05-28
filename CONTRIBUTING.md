# Contributing to Media Search Agent

Thanks for taking the time to look at how to contribute. This project is in
active single-maintainer development with a specific workflow, so the highest-
leverage ways to help are not always "send a PR."

## The highest-leverage contribution right now is opening an issue

Bug reports, feature requests, and "this doesn't work on my machine" reports
are extremely welcome and tend to land in the next release. The current
single-maintainer workflow can absorb that signal quickly.

Particularly useful kinds of issues:

- **Install failures** on a fresh machine, with the install log.
- **Real-world search results that look wrong** — query, expected, actual, and
  enough about your library (rough size, image vs video) to reproduce.
- **Platform traps** — anything that worked on the maintainer's machine but
  fails on yours (Apple Silicon vs Intel, NVIDIA driver versions, WSL2
  edge cases, antivirus interference).
- **Documentation gaps** — anything in the docs that was wrong, missing, or
  confusing when you tried to follow it.

When opening an issue:

- Include the OS + version, hardware (CPU/GPU), and Python version.
- For install issues, attach the install log (path is shown at the end of a
  failed install).
- For search issues, attach a screenshot if the result is visual.

## Pull requests

**Please open an issue first to discuss the change before sending a PR.**

The codebase moves under tight constraints:

- Architecture decisions are documented as ADRs in
  [docs/decisions/](docs/decisions/) — substantive changes must respect or
  amend the relevant ADR.
- Conventions and workflow expectations live in [CLAUDE.md](CLAUDE.md) (the
  agent instruction file, also useful as a contributor reference).
- Non-trivial design questions go through a **spike** first (see
  [docs/AGENTIC_DEVELOPMENT.md](docs/AGENTIC_DEVELOPMENT.md) and the existing
  spikes in [docs/spikes/](docs/spikes/) for examples).

PRs without prior issue discussion frequently conflict with in-flight work or
with decisions documented in ADRs, and are likely to need substantial rework
before merge. Opening an issue first lets us flag those conflicts early.

## A note on AI-assisted contributions

This project is itself built with AI coding agents under a specific workflow
(see [docs/AGENTIC_DEVELOPMENT.md](docs/AGENTIC_DEVELOPMENT.md)). AI assistance
is not a problem per se — but agents working *without* the project's context
(`CLAUDE.md`, the relevant ADRs, the existing patterns) tend to produce PRs
that don't fit.

Two ways to make AI-assisted contributions work:

- **Best:** open an issue describing the problem or idea. If we agree the
  work is in-scope, the project's own workflow can implement it with full
  context, or we can collaborate with you on the design.
- **If you want to send a PR yourself with AI assistance:** read `CLAUDE.md`,
  the relevant ADR(s), and any related spike doc *before* writing code.
  Mention in the PR description what AI you used and what you reviewed.
  PRs that show this context tend to merge; PRs that don't tend to bounce.

This isn't a blanket prohibition on AI-generated code — it's a request that
any contribution (AI-assisted or not) demonstrate enough familiarity with the
project's conventions to not waste reviewer time.

## Dev environment

Supported dev environments are **macOS** and **WSL2 / Linux**. There is no
Windows-native dev path — Windows contributors should work inside a WSL2
Ubuntu shell.

Setup:

```bash
git clone https://github.com/openara-ai/media-search-agent.git
cd media-search-agent
bash scripts/dev-setup.sh
```

Run tests:

```bash
bash scripts/run-tests.sh
```

Run the app locally:

```bash
bash scripts/start.sh
# UI at http://localhost:8000
```

## Workflow expectations

- **Branch from `main`** with a descriptive name (`feature/<topic>` or
  `fix/<topic>`).
- **Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org/)
  — `feat:`, `fix:`, `docs:`, `chore:`, etc.
- **CI must pass** before review. The same checks run locally via
  `bash scripts/run-tests.sh`.
- **One thing per PR** — small, reviewable diffs land faster than large
  refactors.

## Code of conduct

Be respectful and constructive. Disagreements about technical choices are
expected and welcome; personal attacks are not.

---

Thanks for reading this far. If anything here is unclear or out of date,
opening an issue about *this doc* counts as a valid contribution too.
