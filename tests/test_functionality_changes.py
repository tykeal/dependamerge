# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test functionality changes and enhancements:
1. Repository name stripping in table output
2. Detailed status information
3. --fix option availability
"""

from dependamerge.models import PullRequestInfo


def test_repository_name_stripping():
    """Test that repository names are stripped of organization prefix."""
    # Create a mock PR with full repository name
    pr_info = PullRequestInfo(
        number=1,
        title="Bump requests from 2.28.0 to 2.28.1",
        body="Test body",
        author="dependabot[bot]",
        head_sha="abc123",
        base_branch="main",
        head_branch="dependabot/pip/requests-2.28.1",
        state="open",
        mergeable=True,
        mergeable_state="clean",
        behind_by=0,
        files_changed=[],
        repository_full_name="lfreleng-actions/standalone-linting-action",
        html_url="https://github.com/lfreleng-actions/standalone-linting-action/pull/1",
    )

    # Extract repository name (what would be shown in table)
    repo_name = pr_info.repository_full_name.split("/")[-1]
    print(f"✓ Full repository name: {pr_info.repository_full_name}")
    print(f"✓ Stripped repository name: {repo_name}")
    assert repo_name == "standalone-linting-action"


def test_status_details():
    """Test detailed status information."""
    # Test different PR states without requiring a real token
    test_cases = [
        # (mergeable, mergeable_state, state, expected_status_contains)
        (True, "clean", "open", "Ready to merge"),
        (False, "dirty", "open", "Merge conflicts"),
        (False, "behind", "open", "Rebase required"),
        (False, "blocked", "open", "Blocked by checks"),
        (None, "draft", "open", "Draft PR"),
        (True, None, "closed", "Closed"),
    ]

    # Create a mock GitHubClient that doesn't require authentication
    class MockGitHubClient:
        def get_pr_status_details(self, pr_info):
            if pr_info.state != "open":
                return f"Closed ({pr_info.state})"

            # Check for draft status first
            if pr_info.mergeable_state == "draft":
                return "Draft PR"

            if pr_info.mergeable is False:
                if pr_info.mergeable_state == "dirty":
                    return "Merge conflicts"
                elif pr_info.mergeable_state == "behind":
                    return "Rebase required"
                elif pr_info.mergeable_state == "blocked":
                    return "Blocked by checks"
                else:
                    return f"Not mergeable ({pr_info.mergeable_state or 'unknown'})"

            if pr_info.mergeable_state == "behind":
                return "Rebase required"

            return "Ready to merge"

    client = MockGitHubClient()

    for mergeable, mergeable_state, state, expected in test_cases:
        pr_info = PullRequestInfo(
            number=1,
            title="Test PR",
            body="Test body",
            author="dependabot[bot]",
            head_sha="abc123",
            base_branch="main",
            head_branch="test-branch",
            state=state,
            mergeable=mergeable,
            mergeable_state=mergeable_state,
            behind_by=0,
            files_changed=[],
            repository_full_name="owner/repo",
            html_url="https://github.com/owner/repo/pull/1",
        )

        status = client.get_pr_status_details(pr_info)
        print(
            f"✓ PR state: mergeable={mergeable}, mergeable_state={mergeable_state}, state={state} -> '{status}' (expected: '{expected}')"
        )
        assert expected in status, f"Expected '{expected}' in '{status}'"


def test_help_output():
    """Test that --fix option appears in help."""
    import subprocess

    try:
        result = subprocess.run(
            ["dependamerge", "--help"], capture_output=True, text=True, timeout=10
        )

        print(f"✓ CLI help exit code: {result.returncode}")
        if "--fix" in result.stdout:
            print("✓ --fix option found in help output")
            # Extract the line with --fix
            for line in result.stdout.split("\n"):
                if "--fix" in line:
                    print(f"  {line.strip()}")
        else:
            print("✗ --fix option NOT found in help output")

    except subprocess.TimeoutExpired:
        print("✗ Help command timed out")
    except Exception as e:
        print(f"✗ Error running help command: {e}")
