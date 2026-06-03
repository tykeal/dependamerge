<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Dependamerge

<!-- markdownlint-disable MD013 -->

[![Source Code](https://img.shields.io/badge/Source_Code-GitHub?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/dependamerge)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/lfreleng-actions/dependamerge/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/dependamerge)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI](https://img.shields.io/pypi/v/dependamerge.svg?label=PyPI)](https://pypi.org/project/dependamerge/)
[![TestPyPI](https://img.shields.io/pypi/v/dependamerge.svg?label=TestPyPI&pypiBaseUrl=https://test.pypi.org)](https://test.pypi.org/project/dependamerge/)
[![CodeQL](https://github.com/lfreleng-actions/dependamerge/actions/workflows/codeql.yml/badge.svg)](https://github.com/lfreleng-actions/dependamerge/actions/workflows/codeql.yml)

<!-- markdownlint-enable MD013 -->

Command-line tool for the management of pull requests in a GitHub organization
and changes on Gerrit Code Review servers.

<!-- markdownlint-disable MD013 -->

| Command | Description                                                              |
| ------- | ------------------------------------------------------------------------ |
| merge   | Bulk approve/merge pull requests (GitHub) or changes (Gerrit)            |
| close   | Bulk close pull requests across a GitHub organization                    |
| blocked | Reports blocked pull requests in a GitHub organization                   |
| status  | Reports repository statistics for tags, releases, and PRs                |

<!-- markdownlint-enable MD013 -->

## Merge

Bulk approves/merges similar pull requests across different repositories in a
GitHub organisation, or similar changes on a Gerrit Code Review server.

### GitHub Mode

By default, bypasses code owner review requirements to enable automated merging
of dependency updates. Supports common automation tools:

- Dependabot
- pre-commit.ci
- Renovate

Also works for individual GitHub users when provided with an override flag.

Matches pull requests based on a heuristic that considers the criteria:

- Pull requests created by the same author/automation
- Pull requests with the same title/body content
- Pull requests containing the same package updates
- Pull requests changing the same files

### Gerrit Mode

When provided with a Gerrit change URL, the tool will:

- Detect the Gerrit server and authenticate using credentials
- Fetch details of the source change
- Search for similar open changes on the same server
- Apply +2 Code-Review and submit matching changes

Gerrit URL format:

```text
https://gerrit.example.org/c/project/name/+/12345
https://gerrit.example.org/base/c/project/name/+/12345
```

The tool automatically detects whether a URL is for GitHub or Gerrit based on
the URL pattern (`/pull/` for GitHub, `/c/.../+/` for Gerrit).

## Status

Reports repository statistics across a GitHub organization, including:

- Latest tags and releases with synchronization status
- Open and merged pull request counts
- Pull requests affecting action files or workflow configurations
- Separate counts for human contributors and automation tools

Helps track release management and identify repositories needing
attention. Supports both table and JSON output formats.

## Blocked

Lists blocked pull requests across a GitHub organization. Useful when
successive merges have created conflicts or the need to rebase. Also
lists pull requests blocked by branch protection rules, such as those
with failed CI jobs, tests, etc.

## Close

Bulk closes similar pull requests across different repositories in a
GitHub organisation. Works with the same automation tools as merge:

- Dependabot
- pre-commit.ci
- Renovate

Also works for individual GitHub users when provided with an override flag.

Uses the same matching heuristic as merge to find similar PRs. Unlike merge,
it requires PRs to be in the open state (no mergeable state checks needed).

## Overview

Dependamerge provides four main functions:

1. **Finding Blocked PRs**: Check entire GitHub organizations to identify
   pull requests with conflicts, failing checks, or other blocking issues
2. **Automated Merging (GitHub)**: Analyze a source pull request and find
   similar pull requests across all repositories in the same GitHub
   organization, then automatically approve and merge the matching PRs
3. **Automated Merging (Gerrit)**: Analyze a source change on a Gerrit Code
   Review server, find similar open changes, then apply +2 Code-Review and
   submit matching changes
4. **Bulk Closing**: Analyze a source pull request and find similar pull
   requests across all repositories in the same GitHub organization, then
   close all matching open PRs

This saves time on routine dependency updates, maintenance tasks, and
coordinated changes across all repositories while providing visibility into
unmergeable PRs that need attention.

**Works with any pull request or change** regardless of author, automation
tool, or origin. The tool automatically detects whether a URL is for GitHub
or Gerrit based on the URL pattern.

## Features

### Blocked Pull Requests in a GitHub Organisation

- **Comprehensive PR Analysis**: Checks all repositories in a GitHub
  organization for unmergeable pull requests
- **Blocking Reason Detection**: Identifies specific reasons preventing PR
  merges (conflicts, failing checks, blocked reviews)
- **Copilot Integration**: Counts unresolved GitHub Copilot feedback comments
  (column shown when present)
- **Smart Filtering**: Excludes standard code review requirements, focuses on
  technical blocking issues
- **Detailed Reporting**: Provides comprehensive tables and summaries of
  problematic PRs
- **Real-time Progress**: Live progress display shows checking status and
  current operations

### Bulk Approval/Merging of Similar Pull Requests Across Repositories

- **Universal PR Support**: Works with any pull request regardless of author
  or automation tool
- **Smart Matching**: Uses content similarity algorithms to match related PRs
  across repositories
- **Bulk Operations**: Approve and merge related similar PRs with a single
  command
- **Security Features**: SHA-based authentication for non-automation PRs
  ensures authorized bulk merges
- **Interactive Mode by Default**: Preview what changes will apply, then
  optionally proceed with merge

### General Features

- **Rich CLI Output**: Beautiful terminal output with progress indicators and
  tables
- **Real-time Progress**: Live progress updates for both checking and merge
  operations
- **Output Formats**: Support for table and JSON output formats
- **Error Handling**: Graceful handling of API rate limits and repository
  access issues

## Supported Pull Requests

- Any pull request from any author
- Manual pull requests from developers
- Automation tool pull requests (Dependabot, Renovate, etc.)
- Bot-generated pull requests
- Coordinated changes across repositories

## Installation (uv + hatch)

This project uses:

- hatchling + hatch-vcs for dynamic (tag-based) versioning
- uv for environment + dependency management (produces/consumes `uv.lock`)

### Quick Start (Run Without Cloning)

Use `uvx` to run the latest published version directly from PyPI
(no virtualenv management needed):

```bash
# Show help (latest release)
uvx dependamerge --help

# Run a specific tagged release
uvx dependamerge==0.1.0 merge https://github.com/owner/repo/pull/123
```

### Local Development Install

```bash
# 1. Install uv (if not already installed)
# macOS/Linux (script):
curl -LsSf https://astral.sh/uv/install.sh | sh
# or with pipx:
pipx install uv

# 2. Clone the repository
git clone <repository-url>
cd dependamerge

# 3. Create & activate a virtual environment (optional but recommended)
uv venv .venv
source .venv/bin/activate  # (On Windows: .venv\Scripts\activate)

# 4. Install project + dev dependencies (uses dependency group 'dev')
uv sync --group dev
```

The first sync will generate `uv.lock`. Commit that file to ensure reproducible
builds.

### Editable Workflow

`uv sync` installs the project in editable (PEP 660) mode automatically.
After making changes you can run:

```bash
uv run dependamerge --help
```

### Building & Publishing

Dynamic version comes from Git tags (e.g. tag `v0.2.0` → version `0.2.0`):

```bash
# Build wheel + sdist
uv build

# (Optional) Inspect dist/
ls dist/

# Publish to PyPI (ensure you have credentials configured)
uv publish
```

If you build before tagging, a local scheme like `0.0.0+local`
(or similar) may appear—tag first for clean releases.

### Updating / Adding Dependencies

Edit `pyproject.toml` and then:

```bash
uv sync
```

To add a dev dependency:

```bash
uv add --group dev pytest-cov
```

### Running a One-Off Version (Isolation)

```bash
# Run a specific version in an ephemeral environment
uvx dependamerge==0.1.0 --help
```

## Authentication

Dependamerge supports both GitHub and Gerrit platforms, each with different
authentication requirements.

### GitHub Authentication

You need a GitHub personal access token with appropriate permissions. The tool
performs both read and write operations on GitHub repositories and pull
requests.

### Configuring a GitHub Personal Access Token

Dependamerge supports both **classic** and **fine-grained** personal access tokens.

To configure a GitHub personal access token for use with dependamerge, go to:

<https://github.com/>

Then:

Profile → Settings → Developer settings → Personal access tokens

#### Option 1: Fine-Grained Personal Access Tokens (Recommended)

Fine-grained tokens → Generate new token

**Required Repository Permissions:**

- **Contents**: Read and write (for merging PRs and accessing file changes)
- **Pull requests**: Read and write (for creating reviews, approving, and merging)
- **Workflows**: Read and write (for PRs that change GitHub Actions workflows)
- **Administration**: Read access (for reading branch protection rules)
- **Metadata**: Read access (automatically included)

**Required Account Permissions:**

- **Organization members**: Read access (to access organization repositories)

**Repository Access:**

- Select "All repositories" or specify which repositories to access

#### Option 2: Tokens (Classic)

**Required Scopes:**

- `read:org` - Read organization membership, teams, and repositories
- `workflow` - Update GitHub Actions workflows (needed for PRs modifying workflows)

One of the two options below is also needed:

- `public_repo` - Access to public repositories (if working with public repos)
- `repo` - Full control of private repositories (includes all repository permissions)

#### What the tool does with these permissions

- **Read Operations**: Access PR details, file changes, reviews, commits, check
  runs, and repository lists
- **Write Operations**: Create PR reviews (approvals), merge pull requests,
  update PR branches

**Important Notes for Branch Protection:**

- If repositories have **branch protection rules** enabled, these
  requirements may apply:
  - **Required status checks**: All CI/CD workflows must pass before merging
  - **Required reviews**: PRs may need approval from code owners or specific teams
  - **Up-to-date branches**: PRs may need to be current with the base branch
  - **Copilot review resolution**: When using `--dismiss-copilot`, the tool automatically
    handles all review types using dismissal or thread resolution as appropriate
- **Default behavior**: By default, dependamerge uses `--force=code-owners` to bypass
  code owner review requirements for automation PRs
- For repositories with **strict branch protection**, use `--force=protection-rules`
  or `--force=all`, though the token owner may need **admin permissions** on
  individual repositories to bypass certain rules

### Setting Up Authentication

Set the token as an environment variable:

```bash
export GITHUB_TOKEN=your_token_here
```

Or pass it directly to the command using `--token`.

### Permission Verification

To verify your token has the correct permissions:

```bash
# Test basic access
curl -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user

# Test organization access
curl -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/orgs/YOUR_ORG/repos
```

### Gerrit Authentication

Gerrit Code Review servers require HTTP credentials for API access. These are
typically different from your SSO/LDAP login credentials.

#### Obtaining Gerrit HTTP Credentials

1. Log into your Gerrit server web interface
2. Navigate to: **Settings** → **HTTP Credentials**
3. Generate a new HTTP password if you don't have one
4. Note your HTTP username (often your email or username)

#### Setting Up Gerrit Authentication

Set the credentials as environment variables:

```bash
export GERRIT_USERNAME="your_username"
export GERRIT_PASSWORD="your_http_password"
```

Alternative environment variable names are also supported for compatibility:

```bash
export GERRIT_HTTP_USER="your_username"
export GERRIT_HTTP_PASSWORD="your_http_password"
```

**Note:** The `GERRIT_USERNAME`/`GERRIT_PASSWORD` variables take precedence
over `GERRIT_HTTP_USER`/`GERRIT_HTTP_PASSWORD` when you configure both.

#### Gerrit Permission Verification

To verify your Gerrit credentials:

```bash
# Test basic access (replace with your Gerrit server)
curl -u "$GERRIT_USERNAME:$GERRIT_PASSWORD" \
  "https://gerrit.example.org/a/accounts/self"
```

A successful response returns your account details in JSON format (with a
`)]}'\n` XSSI guard prefix that Gerrit adds to all JSON responses).

#### Using .netrc Files

Dependamerge supports loading Gerrit credentials from `.netrc` files, following
the standard format used by curl and other tools.

**Search order:**

1. `.netrc` in the current directory
2. `~/.netrc` in your home directory
3. `~/_netrc` (Windows fallback)

**Example `.netrc` file:**

```text
machine gerrit.onap.org login myuser password mytoken
machine gerrit.opendaylight.org login myuser password anothertoken
```

**CLI options:**

| Option | Description |
| ------ | ----------- |
| `--no-netrc` | Disable .netrc file lookup |
| `--netrc-file PATH` | Use a specific .netrc file |
| `--netrc-optional` | Do not fail if .netrc file is missing (default) |
| `--netrc-required` | Require a .netrc file and fail if missing |

By default, `.netrc` lookup is optional (`--netrc-optional`): if no `.netrc`
file exists, the tool continues and falls back to environment variables.
Use `--netrc-required` to enforce that a `.netrc` file must be present.

When a `.netrc` file is present, the tool loads credentials automatically.
Explicit environment variables or CLI arguments take precedence over `.netrc`
entries.

## Usage

### Closing Pull Requests

Close pull requests across an entire GitHub organization:

```bash
# Close similar PRs from automation tools (dependabot, pre-commit.ci)
dependamerge close https://github.com/myorg/repo1/pull/45

# Close with no confirmation (immediate closing)
dependamerge close https://github.com/myorg/repo1/pull/45 --no-confirm

# Close with custom similarity threshold
dependamerge close https://github.com/myorg/repo1/pull/45 --threshold 0.9

# Close user-generated PRs with override SHA
dependamerge close https://github.com/myorg/repo1/pull/45 --override a1b2c3d4e5f6g7h8
```

The close command will:

- Analyze the provided PR
- Find similar PRs across the organization
- Close all matching PRs that are in the open state
- Skip PRs that are already closed or are drafts

**Note:** Unlike the merge command, the close command does not need to check
mergeable state or branch protection rules. It requires PRs to be in the open
state.

### Repository Status

Report statistics for tags, releases, and pull requests:

```bash
# Basic organization status check
dependamerge status myorganization

# Using full GitHub URL
dependamerge status https://github.com/myorganization/

# Check with JSON output
dependamerge status myorganization --format json

# Disable real-time progress display
dependamerge status myorganization --no-progress
```

The status command will:

- Scan all repositories in the organization
- Report latest tags and releases with sync status indicators
- Count open and merged PRs (split by human/automation)
- Identify PRs affecting action files or workflow configurations

Status icons:

- ✅ Tag has matching release
- ⚠️ Tag exists but no matching release
- ❌ Release is more recent than tag

### Finding Blocked PRs

Find blocked pull requests in an entire GitHub organization:

```bash
# Basic organization check for blocked PRs
dependamerge blocked myorganization

# Using full GitHub URL
dependamerge blocked https://github.com/myorganization/

# Check with JSON output
dependamerge blocked myorganization --format json

# Disable real-time progress display
dependamerge blocked myorganization --no-progress
```

The blocked command will:

- Analyze all repositories in the organization
- Identify PRs with technical blocking issues
- Report blocking reasons (merge conflicts, failing workflows, etc.)
- Count unresolved GitHub Copilot feedback comments (displayed when present)
- Exclude standard code review requirements from blocking reasons

### Merging Pull Requests

The merge command supports both GitHub PRs and Gerrit changes. The platform
is automatically detected from the URL format.

#### GitHub Pull Requests

```bash
dependamerge merge \
  https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

#### Gerrit Changes

```bash
# Set credentials first
export GERRIT_USERNAME="your_username"
export GERRIT_PASSWORD="your_http_password"

# Merge similar changes
dependamerge merge \
  https://gerrit.linuxfoundation.org/infra/c/releng/lftools/+/12345
```

### Optional Security Validation

For extra security, you can use the --override flag with SHA-based validation:

```bash
dependamerge merge https://github.com/owner/repo/pull/123 \
  --override a1b2c3d4e5f6g7h8
```

The SHA hash derives from:

- The PR author's GitHub username
- The first line of the commit message
- This provides an extra layer of validation for sensitive operations

### Interactive Preview Mode

By default, dependamerge runs in interactive mode showing you what PRs the tool
will merge, then prompts you to continue:

```bash
dependamerge merge https://github.com/owner/repo/pull/123
```

**Interactive Flow:**

1. Analyzes and shows similar PRs that the tool will merge
2. Displays merge evaluation results
3. Generates a unique SHA for security validation
4. Prompts you to enter the SHA to proceed with actual merging
5. Merges PRs that appear as "mergeable" in the preview

**Example Output:**

```bash
🔍 Dependamerge Evaluation

✅ Approve/merge: https://github.com/org/repo1/pull/45
⏭️ Skipped: https://github.com/org/repo2/pull/67 [cannot update protected ref]
✅ Approve/merge: https://github.com/org/repo3/pull/89

▶️ Mergeable 2/3 PRs

➡️ To proceed with merging enter: abc123def456
Enter the string above to continue (or press Enter to cancel):

🔨 Merging 2 mergeable pull requests...
✅ Success: https://github.com/org/repo1/pull/45
✅ Success: https://github.com/org/repo3/pull/89

🚀 Final Results: 2 merged, 0 failed
```

### Custom Merge Options

```bash
dependamerge merge https://github.com/owner/repo/pull/123 \
  --threshold 0.9 \
  --merge-method squash \
  --no-fix \
  --no-progress \
  --token your_github_token
```

### Command Options

#### Status Command Options

- `--format TEXT`: Output format - table or json (default: table)
- `--progress/--no-progress`: Show real-time progress updates (default:
  progress)
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)

#### Blocked Command Options

- `--format TEXT`: Output format - table or json (default: table)
- `--progress/--no-progress`: Show real-time progress updates (default:
  progress)
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)

#### Merge Command Options

**General Options:**

- `--no-confirm`: Skip confirmation prompt and merge without delay (default is
  interactive mode)
- `--threshold FLOAT`: Similarity threshold for matching PRs/changes (0.0-1.0,
  default: 0.8)
- `--progress/--no-progress`: Show real-time progress updates (default:
  progress)
- `--verbose`, `-v`: Enable verbose debug logging

**GitHub-Specific Options:**

- `--merge-method TEXT`: Merge method - merge, squash, or rebase (default:
  merge)
- `--no-fix`: Disable automatic fixing of out-of-date branches
  (default: automatic fixing enabled)
- `--dismiss-copilot`: Automatically resolve unresolved GitHub Copilot reviews
  (dismissal + thread resolution)
- `--force TEXT`: Override level for bypassing safety checks - `none` (default),
  `code-owners`, `protection-rules`, or `all`. See [Force Override System](docs/FORCE_OVERRIDE_SYSTEM.md)
  for detailed documentation
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)
- `--override TEXT`: SHA hash for extra security validation

**Gerrit Environment Variables:**

- `GERRIT_USERNAME`: HTTP username for Gerrit authentication
- `GERRIT_PASSWORD`: HTTP password for Gerrit authentication

Fallback variables:

- `GERRIT_HTTP_USER`: HTTP username (fallback)
- `GERRIT_HTTP_PASSWORD`: HTTP password (fallback)

#### Close Command Options

- `--no-confirm`: Skip confirmation prompt and close without preview
- `--threshold FLOAT`: Similarity threshold for matching PRs (0.0-1.0,
  default: 0.8)
- `--progress/--no-progress`: Show real-time progress updates (default:
  progress)
- `--debug-matching`: Show detailed scoring information for PR matching
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)
- `--override TEXT`: SHA hash to override non-automation PR restriction

## How It Works

### Pull Request Processing

1. **Parse Source PR**: Analyzes the provided pull request URL and extracts
   metadata
2. **Organization Check**: Lists all repositories in the same GitHub
   organization
3. **PR Discovery**: Finds all open pull requests in each repository
4. **Content Matching**: Compares PRs using different similarity metrics:
   - Title similarity (normalized to remove version numbers)
   - File change patterns
   - Author matching
5. **Optional Validation**: If `--override` provided, validates SHA for extra
   security
6. **Approval & Merge**: For matching PRs above the threshold:
   - Adds an approval review
   - Merges the pull request
7. **Source PR Merge**: Merges the original source PR that served as the
   baseline

## Similarity Matching

The tool uses different algorithms to determine if PRs are similar:

### Title Normalization

- Removes version numbers (e.g., "1.2.3", "v2.0.0")
- Removes commit hashes
- Removes dates
- Normalizes whitespace

### File Change Analysis

- Compares changed filenames using Jaccard similarity
- Accounts for path normalization
- Ignores version-specific filename differences

### Confidence Scoring

Combines different factors:

- Title similarity score
- File change similarity score
- Author matching (same automation tool)

## Examples

### Example: Finding Blocked PRs

```bash
# Check organization for blocked PRs
dependamerge blocked myorganization

# Get detailed JSON output
dependamerge blocked myorganization --format json > unmergeable_prs.json

# Check without progress display
dependamerge blocked myorganization --no-progress
```

### Example: Automated Merging

#### Dependency Update PR

```bash
# Merge a dependency update across all repos
dependamerge merge https://github.com/myorg/repo1/pull/45
```

#### Documentation Update PR

```bash
# Merge documentation updates
dependamerge merge https://github.com/myorg/repo1/pull/12 --threshold 0.85
```

#### Feature PR with Security Validation

```bash
# Merge with optional security validation
dependamerge merge https://github.com/myorg/repo1/pull/89 \
  --override f1a2b3c4d5e6f7g8
```

### Example: Gerrit Merge

#### Basic Gerrit Change Merge

```bash
# Set up Gerrit credentials
export GERRIT_USERNAME="your_username"
export GERRIT_PASSWORD="your_http_password"

# Merge similar changes on a Gerrit server
dependamerge merge \
  https://gerrit.linuxfoundation.org/infra/c/releng/lftools/+/12345
```

#### Gerrit Change with Custom Threshold

```bash
# Merge with higher similarity threshold
dependamerge merge \
  https://gerrit.example.org/c/project/name/+/67890 \
  --threshold 0.9
```

#### Non-Interactive Gerrit Merge

```bash
# Skip confirmation prompt for automation
dependamerge merge --no-confirm \
  https://gerrit.linuxfoundation.org/infra/c/releng/global-jjb/+/74080
```

### Example: GitHub Merge (continued)

#### Resolving Copilot Comments

The `--dismiss-copilot` flag automatically resolves blocking Copilot reviews
using the most appropriate method:

```bash
# Merge with automatic Copilot review resolution (interactive mode)
dependamerge merge https://github.com/myorg/repo1/pull/67 --dismiss-copilot

# Interactive mode to see which Copilot items the tool will resolve, then
# choose to proceed (default behavior)
dependamerge merge https://github.com/myorg/repo1/pull/67 --dismiss-copilot

# Skip confirmation and merge without delay with Copilot dismissal
dependamerge merge https://github.com/myorg/repo1/pull/67 --dismiss-copilot --no-confirm
```

**Comprehensive Resolution Strategy**: The tool automatically uses the most
appropriate method for each Copilot review:

- ✅ **APPROVED reviews** → Dismissed via GitHub API
- ✅ **CHANGES_REQUESTED reviews** → Dismissed via GitHub API
- ✅ **COMMENTED reviews** → Individual review threads resolved automatically
- ✅ **Automatic fallback** → No manual intervention required

The tool intelligently handles GitHub API limitations by automatically falling
back to thread-level resolution for COMMENTED reviews, ensuring comprehensive
coverage without requiring user intervention.

#### Interactive Preview with Fix Option

```bash
# See what changes will apply (default: fix out-of-date branches)
dependamerge merge https://github.com/myorg/repo1/pull/78 \
  --threshold 0.9 --progress
```

#### Bypassing Branch Protection with Force Levels

The `--force` option provides tiered override levels for bypassing safety checks
when you have appropriate permissions.

```bash
# Bypass code owner review requirements (you are a code owner)
dependamerge merge https://github.com/myorg/repo1/pull/45 --force=code-owners

# Bypass branch protection validation (you have admin/bypass permissions)
dependamerge merge https://github.com/myorg/repo1/pull/67 --force=protection-rules

# Emergency override - attempt merge despite most warnings (use with caution)
dependamerge merge https://github.com/myorg/repo1/pull/89 --force=all
```

**Force Levels**:

- `none` (default): Respect all protections
- `code-owners`: Bypass code owner review requirements
- `protection-rules`: Bypass branch protection checks (requires permissions)
- `all`: Attempt merge despite most warnings (not recommended)

**⚠️ Important**: Force levels bypass tool-level checks. GitHub API will still
enforce actual merge restrictions based on your permissions. In some cases,
and when branch protection rules are in place, this will result in failed merge
attempts.

## Safety Features

### Force Levels

Dependamerge provides configurable safety levels to handle different repository
protection scenarios:

- **`none`**: Respect all protections and requirements
- **`code-owners`**: Bypass code owner review requirements (default)
- **`protection-rules`**: Bypass branch protection checks (requires permissions)
- **`all`**: Attempt merge despite most warnings (not recommended)

The default `code-owners` level allows automated merging of dependency updates
even when repositories require code owner reviews, which is the most common
blocking scenario for automation PRs.

**Examples:**

```bash
# Use full safety (respect all protections)
dependamerge merge https://github.com/owner/repo/pull/123 --force=none

# Default behavior (bypass code owner requirements)
dependamerge merge https://github.com/owner/repo/pull/123

# Bypass branch protection rules (requires admin permissions)
dependamerge merge https://github.com/owner/repo/pull/123 \
  --force=protection-rules

# Force merge despite most warnings (use with extreme caution)
dependamerge merge https://github.com/owner/repo/pull/123 --force=all
```

### For All PRs

- **Mergeable Check**: Verifies PRs are in a mergeable state before attempting
  merge
- **Auto-Fix**: Automatically update out-of-date branches by default
  (use `--no-fix` to disable)
- **Detailed Status**: Shows specific reasons preventing PR merges (conflicts,
  blocked by checks, etc.)
- **Similarity Threshold**: Configurable confidence threshold prevents incorrect
  matches
- **Interactive Mode by Default**: Shows results then lets you choose to
  proceed (use `--no-confirm` to skip)
- **Detailed Logging**: Shows which PRs match and why they match

### Security for All PRs

- **SHA-Based Validation**: Provides unique SHA hash for security
- **Author Isolation**: When using SHA validation, processes PRs from the same
  author as source PR
- **Commit Binding**: SHA changes if commit message changes, preventing replay
  attacks
- **Cross-Author Protection**: When enabled, one author's SHA cannot work for
  another author's PRs

## Enhanced URL Support

The tool now supports GitHub PR URLs with path segments:

```bash
# These URL formats now work:
dependamerge merge https://github.com/owner/repo/pull/123
dependamerge merge https://github.com/owner/repo/pull/123/
dependamerge merge https://github.com/owner/repo/pull/123/files
dependamerge merge https://github.com/owner/repo/pull/123/commits
dependamerge merge https://github.com/owner/repo/pull/123/files/diff
```

This enhancement allows you to copy URLs directly from GitHub's PR pages
without worrying about the specific tab you're viewing.

## Development

### Setup Development Environment

(If you already followed the Installation section, you can skip these repeated
steps.)

```bash
git clone <repository-url>
cd dependamerge
uv venv .venv
source .venv/bin/activate
uv sync --group dev
```

The `dev` dependency group mirrors the legacy `.[dev]` extra.

### Running Tests

```bash
uv run pytest
```

You can pass args as usual:

```bash
uv run pytest -k "similarity and not slow" -vv
```

#### Pre-commit Integration

Tests are automatically integrated into pre-commit hooks and run on every commit.
This project uses [prek](https://github.com/j178/prek) (a faster, Rust-based
drop-in replacement for pre-commit) as the local hook runner:

```bash
# Install prek git hooks (tests will run automatically on commits)
prek install

# Run all checks including tests manually
prek run --all-files

# Run the pytest hook
prek run pytest
```

Note: The pytest hook runs automatically on every commit to ensure code quality.

### Code Quality

```bash
# Format (Black)
uv run black src tests

# Lint (Flake8 – still present)
uv run flake8 src tests

# Type checking
uv run mypy src

# (Optional) Ruff (if/when added)
# uv run ruff check .
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

Apache-2.0 License - see LICENSE file for details.

## Troubleshooting

### Common Issues

#### Authentication Error

```text
Error: GitHub token needed
```

Solution: Set `GITHUB_TOKEN` environment variable or use `--token` flag.

### Permission Error

```text
Failed to fetch organization repositories
```

Solution: Ensure your token has the required permissions:

- **Classic tokens**: `read:org` scope
- **Fine-grained tokens**: "Organization members: Read access" permission

### Write Permission Error

```text
403 Forbidden during merge attempt
```

Solution: Ensure your token has write permissions:

- **Classic tokens**: `repo` scope (or `public_repo` for public repositories)
- **Fine-grained tokens**: "Contents: Read and write" permission

### Pull Request Review Permission Error

```text
Failed to approve PR: Missing 'Pull requests: Read and write' permission
```

Solution: Ensure your token can create PR reviews:

- **Classic tokens**: `repo` scope (includes PR review permissions)
- **Fine-grained tokens**: "Pull requests: Read and write" permission

### Actions/Checks Access Error

```text
Failed to check PR status
```

Solution: Add workflow/actions permissions:

- **Classic tokens**: `workflow` scope
- **Fine-grained tokens**: "Workflows: Read and write" permission

#### No Similar PRs Found

- Check that other repositories have open automation PRs
- Try lowering the similarity threshold with `--threshold 0.7`
- Use interactive mode (default) to see detailed matching information and
  optionally proceed with merge

#### Merge Failures

- Ensure PRs are in mergeable state (no conflicts)
- Check that you have write permissions to the target repositories
- Verify the repository settings permit the merge method

### Gerrit Authentication Error

```text
❌ Gerrit credentials not found in environment.
```

Solution: Set the required environment variables:

```bash
export GERRIT_USERNAME="your_username"
export GERRIT_PASSWORD="your_http_password"
```

Note: These are your **HTTP credentials** from Gerrit, not your SSO/LDAP
password. Generate them in Gerrit under Settings → HTTP Credentials.

### Gerrit API Error

```text
❌ Gerrit authentication failed
```

Solution:

- Verify your HTTP credentials are correct
- Check that you have permission to access the Gerrit server
- Ensure the server URL is correct and accessible
- Try testing with curl:

```bash
curl -u "$GERRIT_USERNAME:$GERRIT_PASSWORD" \
  "https://gerrit.example.org/a/accounts/self"
```

### Gerrit URL Not Recognized

```text
❌ Invalid URL: Cannot determine platform for URL
```

Solution: Ensure your Gerrit URL follows the correct format:

- `https://gerrit.example.org/c/project/name/+/12345`
- `https://gerrit.example.org/base/c/project/name/+/12345`

The URL must contain `/c/` and `/+/` for the tool to recognize it as a Gerrit change.

### Getting Help

- Check the command help (local dev): `uv run dependamerge --help`
- For PyPI usage: `uvx dependamerge --help`
- Enable verbose output with `--verbose` or `-v`
- Review similarity scoring in interactive mode (default behavior)

## Security Considerations

### Credential Protection

Dependamerge handles authentication tokens (GitHub PATs, Gerrit HTTP
credentials) and applies the following layers of protection to prevent
accidental exposure:

- **Token Redaction** — All git operations redact tokens from logs and
  error messages. The `git_ops` module recognises GitHub classic tokens
  (`ghp_`), fine-grained tokens (`github_pat_`), App installation tokens
  (`ghs_`), user-to-server tokens (`ghu_`), GitLab tokens (`glpat-`),
  basic-auth URLs, and `x-access-token` URLs
- **Safe Object Representation** — All classes that store credentials
  (`GitHubClient`, `GitHubAsync`, `AsyncMergeManager`, `AsyncCloseManager`,
  `GerritRestClient`, `GerritCredentials`, `NetrcCredentials`) define
  `__repr__` methods that mask sensitive values
- **Log Hygiene** — Credential values (tokens, passwords, or usernames)
  never appear in log output at any level. Debug logs record credential
  *sources* (e.g., "environment variables", ".netrc") but never the
  credential values themselves
- **Scoped Debug Logging** — The `--verbose` flag enables DEBUG level
  logging for the `dependamerge.*` namespace; third-party libraries
  (including `httpx`, which logs request headers at TRACE level) remain
  at WARNING
- **Exception Sanitisation** — Git command errors redact token patterns
  from all stored attributes (`args_vec`, `stdout`, `stderr`)

### URL Validation

URL hostname checks use `urlparse()`-based exact matching via the
`_host_matches()` function, not substring checks. This prevents bypass
attacks where a crafted hostname (e.g., `evil-github.com.attacker.net`)
could fool a naïve substring check into trusting a malicious host.

### GitHub

- Store GitHub tokens securely (environment variables, not in code)
- Use tokens with minimal required permissions for your use case
- Rotate access tokens periodically
- Review PR changes in interactive preview mode first
- Be cautious with low similarity thresholds
- Consider using repository-specific tokens instead of organisation-wide
  access when possible
- Audit token permissions and revoke unused tokens periodically

### Gerrit

- Store Gerrit HTTP credentials securely (environment variables, not in code)
- Use HTTP credentials generated specifically for API access, not your main
  password
- Regenerate HTTP credentials periodically in Gerrit Settings → HTTP Credentials
- Review changes in interactive preview mode before submitting
- Be aware that the tool performs +2 Code-Review and submit operations using
  your credentials
- Ensure you have appropriate permissions on the Gerrit server before running
  bulk operations
