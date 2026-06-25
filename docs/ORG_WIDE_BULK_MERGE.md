<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 The Linux Foundation -->

# Org/Owner-Wide Bulk Merging — Design

Tracking issue:
[#342](https://github.com/lfreleng-actions/dependamerge/issues/342)

GHE follow-up:
[#343](https://github.com/lfreleng-actions/dependamerge/issues/343)

## Summary

Add a third scope to the `merge` command so that a bare owner URL bulk
merges every in-scope automation pull request across an entire GitHub
organisation (or user account):

```bash
dependamerge merge https://github.com/lfreleng-actions/
```

This hybridises the two existing bulk modes:

- Like **repo merge**, no similarity correlation applies — author
  markers identify automation PRs, rather than a match against a source
  PR.
- Like **repo merge**, merging one PR can change the merge state of
  sibling PRs in the same repository (rebase needed, fresh conflict), so
  the sequencing concerns of the in-repo path apply — now multiplied
  across every repository in the owner.

The novel work is a **striping scheduler** that spreads merge operations
across repositories and guarantees at most one in-flight merge per
repository, structurally avoiding the data-replication races that would
otherwise force injected delays or random retries.

## Scope of this design

In scope:

- New owner-scoped URL parsing and routing.
- Runtime detection of organisation vs user accounts.
- Enumeration of in-scope automation PRs across the owner.
- A striped merge scheduler with per-repo single-flight.
- Full parity with every existing per-PR behaviour and flag.
- GHE scaffolding (carry `host`, centralise base-URL derivation) without
  enabling GHE.

Out of scope (deferred):

- Actually enabling GitHub Enterprise hosts — tracked in #343.
- Routing the existing single-repo flow through the striped scheduler.

## Command surface

The `merge` command grows a third scope; no new subcommand. The URL
shape continues to establish the scope:

| URL shape                                     | Scope       |
| --------------------------------------------- | ----------- |
| `…/owner/repo/pull/123`                       | single PR   |
| `…/owner/repo`                                | whole repo  |
| `…/owner` (and `…/orgs/owner[/repositories]`) | whole owner |

Routing in `merge()` tries three parsers in order: `parse_change_url`
(single PR) first, then `parse_org_url` (owner), then `parse_repo_url`
(repo). Routing tries owner parsing ahead of repo parsing so the
canonical `…/orgs/owner` form does not mis-parse as `owner="orgs"`.
`parse_org_url` is strict — it accepts a bare owner or the
`orgs/owner[/repositories]` forms and rejects everything else, so a
two-segment `owner/repo` URL falls through to `parse_repo_url`. If all
three fail, routing surfaces the most relevant error.

## URL parsing

Add `parse_org_url` returning a new frozen dataclass `ParsedOrgUrl`:

```text
ParsedOrgUrl(source, host, owner, original_url)
```

Parsing rules (comprehensive, not simplified):

- One path segment ⇒ owner scope (`…/owner`).
- The canonical GitHub forms `…/orgs/owner` and
  `…/orgs/owner/repositories` normalise to `owner`.
- Trailing slashes are cosmetic and treated identically to their
  slash-free form (paths are already `rstrip("/")`-ed).
- Disambiguation by segment count: 1 ⇒ owner, 2 ⇒ repo, 3+ with `/pull/`
  ⇒ single PR.

The parsers keep separate, single-purpose contracts (`parse_change_url`,
`parse_repo_url`, `parse_org_url`) rather than overloading one function.

### Host support and the GHE scaffold

The low-level transport (`GitHubAsync`) already accepts `api_url` and
`graphql_url`, so GHE is not a transport problem. For this feature:

- `ParsedOrgUrl` carries `host` as a first-class field. Retrofitting the
  same onto `ParsedRepoUrl` / `ParsedUrl` is desirable for consistency.
- A single centralised helper `derive_api_urls(host) -> (api_url,
  graphql_url)` encodes the dotcom-vs-GHE base-URL rule in one place.
  The github.com path flows through it and returns the existing
  constants.
- A single, well-commented guard that accepts `github.com` alone remains
  at the parser layer — one place to relax when GHE lands. The rejection
  message stays forward-looking ("GitHub Enterprise support is not yet
  enabled"), never implying impossibility.

This PR does **not** thread `host`-derived base URLs through
`GitHubService` / `GitHubClient` / `AsyncMergeManager`; that is #343's
job. The parsed objects already carry the `host` those constructors will
consume.

## Account-type detection (org vs user)

The owner URL is identical for organisations and personal accounts, so
runtime detection determines the account type, rather than the URL:

- Try the `organization(login:)`-rooted repository query first
  (`ORG_REPOS_ONLY` already exists).
- If that field comes back `null` / `NOT_FOUND`, fall back to a new
  `USER_REPOS_ONLY` query rooted at `user(login:)`. The `repositories`
  connection exists on both `Organization` and `User`.
- A cache holds the resolved account type so repeated pagination pages
  do not re-probe.

A small `_iter_owner_repositories(owner)` selects the correct query and
yields repository nodes, mirroring the existing
`_iter_org_repositories`.

## Repository scope

Per repository, in-scope filtering:

- The enumerator skips **archived** repositories (already the
  established behaviour of `_iter_org_repositories`).
- The enumerator skips **forks** by default. The new owner-repos query
  must add `isFork` to its node selection, because `ORG_REPOS_ONLY`
  returns `nameWithOwner` and `isArchived` alone. The README documents
  this. A future `--include-forks` flag could relax it.
- Repositories with no open automation PRs naturally drop out — the
  scheduler acts on the repos that yield PRs.
- No upfront write/merge permission probe (too expensive org-wide).
  Missing permissions surface through the existing per-PR merge-failure
  handling, as repo mode behaves today.

## PR enumeration

Add `GitHubService.fetch_owner_open_prs(owner, *, only_automation=True)`
returning `list[PullRequestInfo]` — the exact type the merge path
consumes.

Implementation reuse:

- Repo fan-out structure (bounded concurrency via `_repo_semaphore`,
  pagination, progress hooks) comes from the existing
  `scan_organization` shape.
- The per-repo "fetch PR pages + convert nodes + filter to automation"
  body is **factored out of `fetch_repo_open_prs` into a shared private
  helper**, so `fetch_repo_open_prs` and `fetch_owner_open_prs` share one
  implementation with no duplicated node-to-`PullRequestInfo` logic.
- Automation classification stays in the single shared bot-identity
  predicate (`bot_identity.is_automation_author`) used by both repo mode
  and `fetch_repo_open_prs`.

### Resilience

Per-repo error isolation mirrors `scan_organization`: a wrapper around
each repo's fetch records a transient failure and lets enumeration
continue. After enumeration a concise summary lists the repositories it
could not read, with their errors, and the run proceeds with the PRs it
gathered. Global rate-limit / secondary-rate-limit errors still
propagate (the API layer owns backoff).

## Striping scheduler (the core of the feature)

Across an owner the flat PR list frequently contains two or more PRs in
the same repository (for example dependabot and pre-commit-ci both
opening PRs in one repo). Merging two PRs in the same repo back-to-back
races GitHub's branch-protection / mergeability propagation. A naive
`asyncio.gather` over the flat list would let the worker pool grab a
handful of PRs from one repo at once, thrashing the refresh/retry path.

The avoidance strategy is **structural, not timing-based**:

1. **Group** the flat PR list by `repository_full_name`.
2. **Run one serial worker per repository.** A single worker owns each
   repository's PRs and processes them strictly one at a time, in order.
   All workers run concurrently under a shared semaphore that bounds
   global concurrency, so progress spreads across distinct repos.
3. **One in-flight PR per repo at a time.** Because a repo's PRs share a
   single serial worker, while one PR moves through approve → rebase →
   merge → post-merge settle, no other PR from that repo can start; the
   semaphore admits PRs from *other* repos instead. (Any tendency for
   consecutive admissions to alternate between repos — a round-robin
   "striping" effect — emerges as a best-effort consequence of how
   CPython wakes semaphore waiters; treat it as an optimisation, not a
   guarantee, because correctness does not depend on it.)
4. **Live mergeability refresh before dispatch** (`repo_scoped` semantics)
   so that when a repo's second PR starts, it re-reads state the first
   PR's merge may have invalidated.

No sleeps and no random retries — the **single-flight-per-repository**
invariant (each repo's serial worker) is the avoidance mechanism, and it
holds regardless of the order in which the semaphore admits waiters. This
complements, and sits above, the existing
`_get_merge_dispatch_lock(owner, repo)`, which serialises the final
`merge_pull_request` API call alone.

### Merge ordering

Before the scheduler runs, the tool orders the flat PR list with the key
`(-pr_count_for_repo, repo_full_name, pr_number)`:

- **Repositories with the most in-scope PRs come first.** A repo with a
  long PR queue takes the longest to drain (each merge can leave the next
  sibling `behind` or `dirty` and trigger a rebase plus a CI wait), so
  starting it earliest gives it the most wall-clock head start.
- **Ascending PR number within a repo.** Dependabot raises PRs in number
  order; merging the oldest first matches the order dependabot expects to
  rebase, which reduces the chance a later PR invalidates an earlier one.

The grouped listing and the merge list both derive from this order, so
the preview mirrors the sequence the tool will follow.

## Wait model and dependabot self-rebase

When a sibling PR merges, dependabot frequently rebases the remaining
open PRs in that repo on its own. While it does, it rewrites the PR body
to include a "Dependabot is rebasing this PR" banner, then re-runs CI.
A PR caught mid-rebase is transiently `dirty`/`behind` and would fail a
naive immediate merge.

The owner-wide path handles this structurally:

1. **Detect the in-progress rebase.** `_dependabot_is_rebasing(body)`
   recognises the banner so the conflict handler does not fire a
   redundant `@dependabot rebase` macro on a PR that is already rebasing.
2. **Approve and arm auto-merge**, then let the striped per-repo worker
   wait for the merge to land in the background while other repos
   progress. Because one serial worker owns each repo, the next sibling
   does not start until the current PR settles.
3. **Bound the whole run** with `--max-wait`.

### The `--max-wait` flag

`--max-wait SECONDS` sets a global wall-clock ceiling on an owner-wide
run (default **900s**, 15 minutes). It clamps every per-PR auto-merge
wait so the run cannot hang indefinitely waiting on slow CI across a
large owner.

- `--max-wait 0` opts into pure fire-and-forget: the tool approves each
  PR, arms auto-merge, reports `AUTO_MERGE_PENDING`, and returns at once
  without blocking. Use this when GitHub should finish the merges after
  the tool exits.
- The flag applies to owner-wide (`stripe=True`) runs. The single-PR and
  single-repo modes ignore it.

## Identity handling

GitHub reports the same App actor under two login forms: REST returns the
suffixed `dependabot[bot]`, GraphQL returns the bare `dependabot`. A
login-equality gate written for one form misclassifies the
other. The owner-wide path surfaced this because its enumeration runs
through GraphQL.

All bot/automation/Copilot identity logic routes through one shared
module, `bot_identity`:

- `canonical_bot_login(login, __typename)` normalises a GraphQL `Bot`
  actor to the REST form at the data boundary, so the rest of the
  codebase sees one canonical login.
- `normalize_bot_login`, `is_dependabot`, `is_copilot`, and
  `is_automation_author` compare logins irrespective of the `[bot]`
  suffix, so a stray non-canonical value cannot disable identity-specific
  handling (dependabot rebase/recreate, Copilot review dismissal,
  automation classification).

### Placement

The scheduler is a `stripe=True` parameter on
`AsyncMergeManager.merge_prs_parallel`. When set, the round-robin +
per-repo single-flight scheduler takes over from the flat
`asyncio.gather` loop. It lives in `AsyncMergeManager` because it needs
the per-repo state, the semaphore, the progress tracker, and the
dispatch lock that already live there. The existing single-PR and repo
flows stay untouched to keep the blast radius small.

### Concurrency

Global concurrency defaults to 10, effectively bounded by
`min(10, number_of_distinct_repos_with_prs)` — single-flight-per-repo
means more workers than repos cannot help. The underlying `GitHubAsync`
owns request-level throttling (`max_concurrency=20`,
`requests_per_second=8`, adaptive backoff), so the worker pool need not
be conservative for rate-limit reasons. No new CLI flag.

## CLI handler

A thin `_handle_org_merge(parsed_org, ctx)` mirrors `_handle_repo_merge`:

1. Guard host to github.com (the single GHE choke point).
2. Initialise the GitHub client / token; reuse `_check_merge_permissions`.
3. List automation PRs via `fetch_owner_open_prs`.
4. Build the flat `(PullRequestInfo, None)` work list.
5. Preview / merge via `_run_parallel_merge(..., stripe=True,
   repo_scoped=True)`.

The handler populates `_MergeContext` identically to the repo handler, so
every per-PR behaviour and flag — GitHub2Gerrit detection/submission,
`--force` levels, `--dismiss-copilot`, branch rebasing, per-repo
merge-method resolution, `--include-human-prs` — applies with **full
parity** and no org-specific special-casing.

## User experience

- **Two-phase progress**, reusing existing tracker mechanisms:
  - Enumeration: "🔍 Scanning `<owner>` repositories for automation PRs"
    using the repos fraction (the owner-repos enumerator publishes
    `totalCount` on its first page).
  - Merge: a fresh tracker with `set_total_prs(...)` and the "▶️ Merging
    PRs" label; the striped scheduler's per-repo hooks
    (`start_repository` / `complete_repository`) stay meaningful as PRs
    span repos across the owner.
- **Listing grouped by repository** — a header per repo with its PRs
  beneath, plus per-repo automation/human counts and a grand total, so a
  large owner-wide list stays scannable.
- **Confirmation** reuses the preview-then-SHA pattern, with the token
  derived from owner + mergeable count (for example
  `org-merge:{owner}:{count}`), mirroring the existing `repo-merge:`
  token. No arbitrary size cap — the preview shows full scope before any
  merge, so the SHA gate suffices.
- **`--include-human-prs`** works owner-wide for consistency, gated by
  the same "type yes" human-PR confirmation, with a warning that human
  PRs across the entire owner are in scope.
- **Empty result** prints "No open automation PRs found in `<owner>`" and
  exits 0.

## Testing

- `test_url_parser.py` — `parse_org_url` cases: bare `<owner>`, trailing
  slash, `orgs/<owner>`, `…/orgs/<owner>/repositories`, non-github.com
  rejection, and 1-vs-2-vs-3+ segment disambiguation.
- `test_org_merge.py` (new) — mirrors `test_repo_merge.py`: owner
  enumeration with org-path and user-path fallback (mocked
  `organization=null`), fork/archived exclusion, automation filtering,
  empty-owner messaging, per-repo error isolation, grouped listing.
- `test_stripe_scheduler.py` (new) — the load-bearing guarantee:
  round-robin interleave ordering and the invariant that **no two PRs
  from the same repo are ever in flight simultaneously** under the
  striped scheduler, asserted with a deterministic fake that records
  concurrent in-flight repos and fails if any repo's count exceeds 1.
- Shared-helper tests proving `fetch_owner_open_prs` and
  `fetch_repo_open_prs` have no behavioural drift.

## Documentation

- New README "Features" subsection: "Org/Owner-Wide Bulk Merging".
- Extend "Enhanced URL Support" with the `<owner>` / `orgs/<owner>` /
  `…/repositories` forms.
- Document fork + archived exclusion explicitly.
- Document the striping / single-flight sequencing behaviour at a high
  level.
- Update the `merge` command help/docstring and Usage examples to show
  `dependamerge merge https://github.com/<owner>`.

## Reused components

<!-- markdownlint-disable MD013 -->

| Concern                      | Reused component                                        |
| ---------------------------- | ------------------------------------------------------- |
| Repo fan-out / pagination    | `scan_organization` structure, `_iter_org_repositories` |
| Per-repo PR fetch + filter   | shared helper extracted from `fetch_repo_open_prs`      |
| Automation classification    | `bot_identity.is_automation_author` predicate           |
| Per-repo merge serialisation | `_get_merge_dispatch_lock`                              |
| Live mergeability refresh    | `repo_scoped` path in `AsyncMergeManager`               |
| Merge orchestration          | `_run_parallel_merge` / `merge_prs_parallel`            |
| Progress display             | `MergeProgressTracker` (repos + PRs fractions)          |
| Confirmation                 | preview-then-SHA pattern (`repo-merge` token)           |
| Per-PR flags / behaviours    | `_MergeContext` → `AsyncMergeManager`                   |

<!-- markdownlint-enable MD013 -->
