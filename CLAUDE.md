# CLAUDE.md

> Read this at the start of every session. It captures the generic conventions,
> guardrails, and workflow expectations that don't change session-to-session and
> aren't specific to any one project structure. Project-specific state (current
> phase, key file locations, ADR summary) **and** project-specific structural
> conventions (doc/script placement rules, doc maintenance routines) live in a
> private companion file — see the bottom of this doc.

## Dev Environment vs End-User Scripts

Build and dev scripts should be portable across the supported dev environments:

- **Build and dev scripts** (`build.sh`, CI workflows): always bash/sh, never PowerShell — bash works on macOS, Linux, and WSL2.
- **End-user scripts** (`install.ps1`, `start.ps1`): PowerShell 5.1 when the user's machine is Windows.

Do not create `.ps1` files for developer tasks. If a script is needed for the dev workflow, write it in bash even if the eventual output targets Windows.

## PowerShell Compatibility (end-user scripts only)

PS1 files run on end-users' Windows machines, so:

- Must be compatible with Windows PowerShell 5.1.
- Prefer ASCII text and LF line endings for `*.ps1`.
- Do not use PowerShell 7-only syntax or cmdlets unless explicitly gated.
- When editing `*.ps1`, validate with PSScriptAnalyzer (run via `pwsh` in WSL2 or directly on Windows).

## Git Workflow (follow this without being asked)

**Branches**
- Never work directly on `main` — always confirm the current branch at the start of a session
- Branch naming: `feature/<short-description>` or `fix/<short-description>`

**Commits**
- Stage named files only — never `git add -A` or `git add .`
- Before committing, run the tests that cover the changed files. The private companion file lists the specific test-to-area mappings for this project.
- Draft the commit message and present the staged diff to the developer before committing
- Wait for developer go-ahead before running `git commit`
- Commit message format:
  ```
  <type>(<scope>): <short summary>

  <body — what changed and why; reference phase and ADR if relevant>

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

**Push and PR**
- Never push or create a PR without the developer explicitly asking
- When asked to create a PR, use `gh pr create` with the PR template — fill in all sections
- Include CI status, what was tested, and the validation checklist

**Responding to PR review feedback**
- Before writing any code, triage every comment with a priority and share the triage list with the developer for confirmation:
  - **P1** — broken / unsafe behaviour, data loss, security, regression of an existing feature
  - **P2** — actively surprises the user or breaks their stated intent (e.g. dismissed UI reopens itself)
  - **P3** — polish / defensive hardening / cosmetic confusion that few users will hit
  - If the reviewer already labelled a priority (Codex emits P1/P2/P3 badges), keep theirs; only assign one when missing
  - Wait for the developer to confirm or adjust the triage before starting any fix — they may want to drop P3s or split work across PRs
- When asked to respond to PR review comments: make the code fixes first, get developer approval, commit, then post the GitHub replies — so each reply can reference the fix commit SHA
- If a comment needs no code change (explaining a decision, deferring an item), reply immediately without waiting for a commit

**Merge**
- Never merge to main — that is always a developer action
- After the developer confirms a merge: run the post-merge doc-maintenance routines defined in the private companion file (e.g. phase tracking updates)

**Never do**
- `git push --force`
- `git commit --amend` on a commit already pushed
- `git reset --hard` without explicit developer instruction
- Skip pre-commit hook (`--no-verify`)

---

## For maintainers of this repo

If a file exists at `internal/docs/CLAUDE-private.md`, **read it next before responding**. It contains everything that's specific to this codebase: the project's structural conventions (doc/script placement, doc maintenance routines), current phase, runtime details, key file locations, ADR summary, and do-not-change rules. The public `CLAUDE.md` above covers only the conventions that aren't project-specific; the private companion is where you'll find the rules that depend on this repo's directory layout and current state.

If you're using this `CLAUDE.md` as a starter for your own project, delete the section above and customize from here.
