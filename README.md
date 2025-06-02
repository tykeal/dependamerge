# Dependamerge

Automatically merge pull requests created by automation tools (like Dependabot, pre-commit.ci, Renovate) across GitHub organizations.

## Overview

Dependamerge analyzes a source pull request from an automation tool and finds similar pull requests across all repositories in the same GitHub organization. It then automatically approves and merges the matching PRs, saving time on routine dependency updates and automated maintenance tasks.

## Features

- **Automated PR Detection**: Identifies pull requests created by popular automation tools
- **Smart Matching**: Uses content similarity algorithms to match related PRs across repositories
- **Bulk Operations**: Approve and merge multiple similar PRs with a single command
- **Dry Run Mode**: Preview what would be changed without making any modifications
- **Rich CLI Output**: Beautiful terminal output with progress indicators and tables

## Supported Automation Tools

- Dependabot
- pre-commit.ci
- Renovate
- GitHub Actions
- Allcontributors

## Installation

```bash
# Install from source
git clone <repository-url>
cd dependamerge
pip install -e .

# Or install dependencies directly
pip install typer requests PyGithub rich pydantic
```

## Authentication

You need a GitHub personal access token with appropriate permissions:

1. Go to GitHub Settings → Developer settings → Personal access tokens
2. Create a token with these scopes:
   - `repo` (for private repositories)
   - `public_repo` (for public repositories)
   - `read:org` (to list organization repositories)

Set the token as an environment variable:

```bash
export GITHUB_TOKEN=your_token_here
```

Or pass it directly to the command using `--token`.

## Usage

### Basic Usage

```bash
dependamerge https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

### Dry Run (Preview Mode)

```bash
dependamerge https://github.com/owner/repo/pull/123 --dry-run
```

### Custom Options

```bash
dependamerge https://github.com/owner/repo/pull/123 \
  --threshold 0.9 \
  --merge-method squash \
  --token your_github_token
```

### Command Options

- `--dry-run`: Show what would be done without making changes
- `--threshold FLOAT`: Similarity threshold for matching PRs (0.0-1.0, default: 0.8)
- `--merge-method TEXT`: Merge method - merge, squash, or rebase (default: merge)
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)

## How It Works

1. **Parse Source PR**: Analyzes the provided pull request URL and extracts metadata
2. **Validation**: Ensures the PR is from a recognized automation tool
3. **Organization Scan**: Lists all repositories in the same GitHub organization
4. **PR Discovery**: Finds all open pull requests in each repository
5. **Content Matching**: Compares PRs using multiple similarity metrics:
   - Title similarity (normalized to remove version numbers)
   - File change patterns
   - Author matching
6. **Approval & Merge**: For matching PRs above the threshold:
   - Adds an approval review
   - Merges the pull request

## Similarity Matching

The tool uses several algorithms to determine if PRs are similar:

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
Combines multiple factors:
- Title similarity score
- File change similarity score
- Author matching (same automation tool)

## Examples

### Dependabot PR
```bash
# Merge a Dependabot dependency update across all repos
dependamerge https://github.com/myorg/repo1/pull/45
```

### pre-commit.ci PR
```bash
# Merge pre-commit hook updates
dependamerge https://github.com/myorg/repo1/pull/12 --threshold 0.85
```

### Dry Run with Custom Threshold
```bash
# See what would be merged with 90% similarity requirement
dependamerge https://github.com/myorg/repo1/pull/78 --dry-run --threshold 0.9
```

## Safety Features

- **Automation-Only**: Only processes PRs from recognized automation tools
- **Mergeable Check**: Verifies PRs are in a mergeable state before attempting merge
- **Similarity Threshold**: Configurable confidence threshold prevents incorrect matches
- **Dry Run Mode**: Always test with `--dry-run` first
- **Detailed Logging**: Shows exactly what PRs were found and why they matched

## Development

### Setup Development Environment

```bash
git clone <repository-url>
cd dependamerge
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
```

### Code Quality

```bash
# Format code
black src tests

# Lint code
flake8 src tests

# Type checking
mypy src
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

MIT License - see LICENSE file for details.

## Troubleshooting

### Common Issues

**Authentication Error**
```
Error: GitHub token is required
```
Solution: Set `GITHUB_TOKEN` environment variable or use `--token` flag.

**Permission Error**
```
Failed to fetch organization repositories
```
Solution: Ensure your token has `read:org` scope.

**No Similar PRs Found**
- Check that other repositories have open automation PRs
- Try lowering the similarity threshold with `--threshold 0.7`
- Use `--dry-run` to see detailed matching information

**Merge Failures**
- Ensure PRs are in mergeable state (no conflicts)
- Check that you have write permissions to the target repositories
- Verify the merge method is allowed in the repository settings

### Getting Help

- Check the command help: `dependamerge --help`
- Enable verbose output with environment variables
- Review the similarity scoring in dry-run mode

## Security Considerations

- Store GitHub tokens securely (environment variables, not in code)
- Use tokens with minimal required permissions
- Regularly rotate access tokens
- Review what PRs will be merged in dry-run mode first
- Be cautious with low similarity thresholds
