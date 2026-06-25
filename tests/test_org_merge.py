# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the owner-wide (org/user) bulk merge feature."""

import hashlib
import re
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from dependamerge.cli import app
from dependamerge.github_service import GitHubService
from dependamerge.models import FileChange, PullRequestInfo
from dependamerge.url_parser import (
    ChangeSource,
    UrlParseError,
    derive_api_urls,
    parse_org_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _mock_asyncio_run(side_effects: list[object]):
    """Mock asyncio.run that closes coroutines to avoid RuntimeWarning."""
    call_index = 0

    def _side_effect(coro):
        nonlocal call_index
        coro.close()
        if call_index < len(side_effects):
            result = side_effects[call_index]
            call_index += 1
            return result
        raise AssertionError(
            f"Unexpected asyncio.run call #{call_index + 1} "
            f"(only {len(side_effects)} side-effects provided)"
        )

    return _side_effect


def _make_pr(
    number: int,
    author: str = "dependabot[bot]",
    title: str = "Bump foo from 1.0 to 2.0",
    repo: str = "owner/repo",
) -> PullRequestInfo:
    return PullRequestInfo(
        number=number,
        title=title,
        body="Automated dependency update",
        author=author,
        head_sha="abc123",
        base_branch="main",
        head_branch="dependabot/npm_and_yarn/foo-2.0",
        state="open",
        mergeable=True,
        mergeable_state="clean",
        behind_by=0,
        files_changed=[
            FileChange(
                filename="package.json",
                additions=1,
                deletions=1,
                changes=2,
                status="modified",
            )
        ],
        repository_full_name=repo,
        html_url=f"https://github.com/{repo}/pull/{number}",
    )


# ---------------------------------------------------------------------------
# URL parser: parse_org_url
# ---------------------------------------------------------------------------


class TestParseOrgUrl:
    def test_bare_owner(self):
        parsed = parse_org_url("https://github.com/lfreleng-actions")
        assert parsed.owner == "lfreleng-actions"
        assert parsed.host == "github.com"
        assert parsed.source is ChangeSource.GITHUB
        assert parsed.is_github

    def test_trailing_slash_identical(self):
        without = parse_org_url("https://github.com/acme")
        with_slash = parse_org_url("https://github.com/acme/")
        assert without.owner == with_slash.owner == "acme"

    def test_without_scheme(self):
        parsed = parse_org_url("github.com/acme")
        assert parsed.owner == "acme"

    def test_http_scheme(self):
        parsed = parse_org_url("http://github.com/acme")
        assert parsed.owner == "acme"

    def test_orgs_canonical_form(self):
        parsed = parse_org_url("https://github.com/orgs/acme")
        assert parsed.owner == "acme"

    def test_orgs_repositories_form(self):
        parsed = parse_org_url("https://github.com/orgs/acme/repositories")
        assert parsed.owner == "acme"

    def test_orgs_repositories_trailing_slash(self):
        parsed = parse_org_url("https://github.com/orgs/acme/repositories/")
        assert parsed.owner == "acme"

    def test_preserves_original_url(self):
        url = "https://github.com/acme/"
        parsed = parse_org_url(url)
        assert parsed.original_url == url

    def test_owner_with_dashes(self):
        parsed = parse_org_url("https://github.com/lf-releng-actions")
        assert parsed.owner == "lf-releng-actions"

    def test_repo_url_rejected(self):
        # owner/repo is a repository scope, not owner-wide.
        with pytest.raises(UrlParseError):
            parse_org_url("https://github.com/acme/widget")

    def test_pr_url_rejected(self):
        with pytest.raises(UrlParseError):
            parse_org_url("https://github.com/acme/widget/pull/5")

    def test_orgs_only_rejected(self):
        # /orgs with no owner is not a valid owner URL.
        with pytest.raises(UrlParseError):
            parse_org_url("https://github.com/orgs")

    def test_empty_raises(self):
        with pytest.raises(UrlParseError):
            parse_org_url("")

    def test_whitespace_only_raises(self):
        with pytest.raises(UrlParseError):
            parse_org_url("   ")

    def test_ghe_host_rejected(self):
        with pytest.raises(UrlParseError) as exc:
            parse_org_url("https://github.enterprise.com/acme")
        assert "github.com" in str(exc.value)

    def test_non_github_host_rejected(self):
        with pytest.raises(UrlParseError):
            parse_org_url("https://gitlab.com/acme")

    def test_github_subdomain_accepted(self):
        parsed = parse_org_url("https://api.github.com/acme")
        assert parsed.owner == "acme"

    def test_case_insensitive_host(self):
        parsed = parse_org_url("https://GitHub.com/Acme")
        assert parsed.host == "github.com"
        assert parsed.owner == "Acme"

    def test_leading_whitespace(self):
        parsed = parse_org_url("  https://github.com/acme  ")
        assert parsed.owner == "acme"

    def test_frozen(self):
        parsed = parse_org_url("https://github.com/acme")
        with pytest.raises(AttributeError):
            parsed.owner = "other"  # type: ignore[misc]


class TestDeriveApiUrls:
    def test_github_dotcom(self):
        api, gql = derive_api_urls("github.com")
        assert api == "https://api.github.com"
        assert gql == "https://api.github.com/graphql"

    def test_github_subdomain(self):
        api, gql = derive_api_urls("api.github.com")
        assert api == "https://api.github.com"

    def test_ghe_host_scaffold(self):
        api, gql = derive_api_urls("ghe.example.com")
        assert api == "https://ghe.example.com/api/v3"
        assert gql == "https://ghe.example.com/api/graphql"

    def test_case_insensitive(self):
        api, _ = derive_api_urls("GitHub.com")
        assert api == "https://api.github.com"

    def test_empty_host_raises(self):
        with pytest.raises(ValueError):
            derive_api_urls("")

    def test_whitespace_host_raises(self):
        # A whitespace-only host would otherwise produce "https:///api/v3".
        with pytest.raises(ValueError):
            derive_api_urls("   ")


# ---------------------------------------------------------------------------
# CLI routing: owner URL -> _handle_org_merge
# ---------------------------------------------------------------------------

_PERMS_OK = {
    "approve": {"has_permission": True},
    "merge": {"has_permission": True},
    "branch_protection": {"has_permission": True},
}


class TestMergeOrgUrl:
    runner: CliRunner = CliRunner()

    def setup_method(self):
        self.runner = CliRunner()

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_owner_url_routes_to_org_handler(self, mock_asyncio_run, mock_client_class):
        auto_pr = _make_pr(1, repo="acme/widget")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                ([auto_pr], []),  # fetch_owner_open_prs -> (prs, errors)
                _PERMS_OK,  # permissions check (deferred until after scan)
                [],  # parallel merge preview
            ]
        )

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        out = _strip_ansi(result.stdout)
        assert "Owner mode" in out
        assert "acme" in out

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_owner_url_no_prs(self, mock_asyncio_run, mock_client_class):
        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                ([], []),  # no PRs, no errors (perms check is skipped)
            ]
        )

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        out = _strip_ansi(result.stdout)
        assert "No open" in out
        assert "PRs found" in out

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_owner_url_groups_by_repo(self, mock_asyncio_run, mock_client_class):
        prs = [
            _make_pr(1, repo="acme/alpha"),
            _make_pr(2, repo="acme/alpha"),
            _make_pr(1, repo="acme/beta"),
        ]

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run([(prs, []), _PERMS_OK, []])

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        out = _strip_ansi(result.stdout)
        # Per-repository grouped headers.
        assert "acme/alpha" in out
        assert "acme/beta" in out
        assert "2 repositories" in out

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_owner_url_surfaces_scan_errors(self, mock_asyncio_run, mock_client_class):
        auto_pr = _make_pr(1, repo="acme/widget")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run(
            [
                ([auto_pr], ["Error scanning repository acme/broken: boom"]),
                _PERMS_OK,
                [],
            ]
        )

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        out = _strip_ansi(result.stdout)
        assert "could not be scanned" in out
        assert "acme/broken" in out

    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_orgs_canonical_url_routes_to_org_handler(
        self, mock_asyncio_run, mock_client_class
    ):
        # /orgs/acme must NOT be mis-parsed as repo owner="orgs".
        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run([([], [])])

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/orgs/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        out = _strip_ansi(result.stdout)
        assert "Owner mode" in out
        # The scanned owner is "acme", not "orgs".
        assert "scanning acme" in out

    def test_owner_url_help_mentions_owner(self):
        result = self.runner.invoke(app, ["merge", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "owner" in out.lower()

    @patch("dependamerge.cli._check_merge_permissions")
    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_permission_check_uses_concrete_repo(
        self, mock_asyncio_run, mock_client_class, mock_check_perms
    ):
        # Regression: the owner-wide permission check must probe a real
        # repository.  check_token_permissions reports every operation as
        # missing when handed an empty repo, which would abort every
        # owner-wide run.  Capture ctx.repo_name at call time and assert
        # it names the representative repo, not "".
        auto_pr = _make_pr(1, repo="acme/widget")

        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        # Enumeration runs first; the deferred permission check follows.
        mock_asyncio_run.side_effect = _mock_asyncio_run([([auto_pr], []), []])

        captured: dict[str, str] = {}

        def _capture(ctx):
            captured["repo_name"] = ctx.repo_name

        mock_check_perms.side_effect = _capture

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        mock_check_perms.assert_called_once()
        assert captured["repo_name"] == "widget"

    @patch("dependamerge.cli._check_merge_permissions")
    @patch("dependamerge.cli.GitHubClient")
    @patch("dependamerge.cli.asyncio.run")
    def test_permission_check_skipped_when_no_prs(
        self, mock_asyncio_run, mock_client_class, mock_check_perms
    ):
        # With nothing to merge there is no representative repo, so the
        # permission check must not run at all.
        mock_client = Mock()
        mock_client.token = "test_token"
        mock_client_class.return_value = mock_client

        mock_asyncio_run.side_effect = _mock_asyncio_run([([], [])])

        result = self.runner.invoke(
            app,
            ["merge", "https://github.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.stdout}"
        mock_check_perms.assert_not_called()

    def test_ghe_owner_url_surfaces_owner_wide_message(self):
        # Regression: an owner-shaped URL on a non-github host (e.g. GHE)
        # must surface parse_org_url's actionable "only supported for
        # github.com … use a direct PR URL" rejection, not the generic
        # parse_change_url "cannot determine platform" message.
        result = self.runner.invoke(
            app,
            ["merge", "https://ghe.example.com/acme", "--token", "test_token"],
        )

        assert result.exit_code == 1
        out = _strip_ansi(result.stdout)
        assert "❌ Invalid URL:" in out
        assert "Owner-wide" in out
        assert "cannot determine platform" not in out.lower()

    def test_ghe_non_owner_url_keeps_platform_guidance(self):
        # A non-owner-shaped URL on a non-github host keeps the
        # platform-agnostic parse_change_url guidance (which also covers
        # Gerrit), rather than the owner-wide github.com-only message.
        result = self.runner.invoke(
            app,
            [
                "merge",
                "https://gerrit.example.org/c/project/sub/extra",
                "--token",
                "test_token",
            ],
        )

        assert result.exit_code == 1
        out = _strip_ansi(result.stdout)
        assert "❌ Invalid URL:" in out
        assert "Owner-wide" not in out


class TestOrgConfirmationHash:
    """The owner confirmation token is deterministic and scoped."""

    def test_hash_is_deterministic(self):
        combined = "org-merge:acme:3"
        h1 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        assert h1 == h2

    def test_hash_varies_with_owner(self):
        a = hashlib.sha256(b"org-merge:acme:3").hexdigest()[:16]
        b = hashlib.sha256(b"org-merge:other:3").hexdigest()[:16]
        assert a != b

    def test_hash_varies_with_count(self):
        a = hashlib.sha256(b"org-merge:acme:3").hexdigest()[:16]
        b = hashlib.sha256(b"org-merge:acme:4").hexdigest()[:16]
        assert a != b


# ---------------------------------------------------------------------------
# Service: fetch_owner_open_prs (fan-out, error isolation, account type)
# ---------------------------------------------------------------------------


class TestFetchOwnerOpenPrs:
    @pytest.mark.asyncio
    async def test_fans_out_across_repositories(self):
        svc = GitHubService(token="test_token")

        async def fake_iter(owner):
            for name in ("acme/alpha", "acme/beta"):
                yield {"nameWithOwner": name}

        async def fake_collect(owner, repo, *, only_automation):
            return [_make_pr(1, repo=f"{owner}/{repo}")]

        svc._iter_owner_repositories = fake_iter  # type: ignore[assignment]
        svc._collect_repo_open_prs = fake_collect  # type: ignore[assignment]

        prs, errors = await svc.fetch_owner_open_prs("acme")
        await svc.close()

        repos = sorted(pr.repository_full_name for pr in prs)
        assert repos == ["acme/alpha", "acme/beta"]
        assert errors == []

    @pytest.mark.asyncio
    async def test_per_repo_error_isolation(self):
        svc = GitHubService(token="test_token")

        async def fake_iter(owner):
            for name in ("acme/good", "acme/bad"):
                yield {"nameWithOwner": name}

        async def fake_collect(owner, repo, *, only_automation):
            if repo == "bad":
                raise RuntimeError("kaboom")
            return [_make_pr(1, repo=f"{owner}/{repo}")]

        svc._iter_owner_repositories = fake_iter  # type: ignore[assignment]
        svc._collect_repo_open_prs = fake_collect  # type: ignore[assignment]

        prs, errors = await svc.fetch_owner_open_prs("acme")
        await svc.close()

        # The good repo's PRs survive; the bad repo is recorded as an error.
        assert [pr.repository_full_name for pr in prs] == ["acme/good"]
        assert len(errors) == 1
        assert "acme/bad" in errors[0]
        assert "kaboom" in errors[0]

    @pytest.mark.asyncio
    async def test_per_repo_error_marks_repository_complete(self):
        # Regression for the Copilot finding: on a per-repo error the
        # error path called start_repository + add_error but never
        # complete_repository, so the completed-repositories counter
        # stalled (progress fraction stuck < 100%) and the current
        # repo/operation stayed stale.  Both the good and the bad repo
        # must now be marked complete.
        completed: list[int] = []
        errors_added = 0

        class _Progress:
            def start_repository(self, name):  # noqa: D401
                pass

            def update_operation(self, msg):
                pass

            def complete_repository(self, count):
                completed.append(count)

            def add_error(self):
                nonlocal errors_added
                errors_added += 1

        svc = GitHubService(token="test_token")
        svc._progress = _Progress()  # type: ignore[assignment]

        async def fake_iter(owner):
            for name in ("acme/good", "acme/bad"):
                yield {"nameWithOwner": name}

        async def fake_collect(owner, repo, *, only_automation):
            if repo == "bad":
                raise RuntimeError("kaboom")
            return [_make_pr(1, repo=f"{owner}/{repo}")]

        svc._iter_owner_repositories = fake_iter  # type: ignore[assignment]
        svc._collect_repo_open_prs = fake_collect  # type: ignore[assignment]

        prs, errors = await svc.fetch_owner_open_prs("acme")
        await svc.close()

        assert [pr.repository_full_name for pr in prs] == ["acme/good"]
        assert len(errors) == 1
        assert errors_added == 1
        # Both repositories (success and failure) are marked complete, so
        # the counter reaches the repository total; the failed repo adds
        # 0 to the unmergeable tally.
        assert sorted(completed) == [0, 1]

    @pytest.mark.asyncio
    async def test_rate_limit_propagates(self):
        from dependamerge.github_async import RateLimitError

        svc = GitHubService(token="test_token")

        async def fake_iter(owner):
            yield {"nameWithOwner": "acme/alpha"}

        async def fake_collect(owner, repo, *, only_automation):
            raise RateLimitError("primary rate limit")

        svc._iter_owner_repositories = fake_iter  # type: ignore[assignment]
        svc._collect_repo_open_prs = fake_collect  # type: ignore[assignment]

        with pytest.raises(RateLimitError):
            await svc.fetch_owner_open_prs("acme")
        await svc.close()

    @pytest.mark.asyncio
    async def test_resolve_owner_root_org(self):
        svc = GitHubService(token="test_token")

        async def fake_graphql(query, variables):
            return {"organization": {"repositories": {"nodes": []}}}

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        root_key, _query = await svc._resolve_owner_root("acme")
        await svc.close()
        assert root_key == "organization"

    @pytest.mark.asyncio
    async def test_resolve_owner_root_user_fallback(self):
        svc = GitHubService(token="test_token")

        async def fake_graphql(query, variables):
            # organization root is null -> owner is a user account
            return {"organization": None}

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        root_key, _query = await svc._resolve_owner_root("someuser")
        await svc.close()
        assert root_key == "user"

    @pytest.mark.asyncio
    async def test_resolve_owner_root_user_fallback_on_not_found_error(self):
        # Against real GitHub, organization(login: <user>) returns a
        # NOT_FOUND GraphQL error (data.organization = null), which
        # GitHubAsync.graphql surfaces as GraphQLError.  That must be
        # treated as "not an org" and fall back to the user root.
        from dependamerge.github_async import GraphQLError

        svc = GitHubService(token="test_token")

        async def fake_graphql(query, variables):
            raise GraphQLError(
                '[{"type": "NOT_FOUND", "path": ["organization"], '
                '"message": "Could not resolve to an Organization with '
                "the login of 'someuser'.\"}]"
            )

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        root_key, _query = await svc._resolve_owner_root("someuser")
        await svc.close()
        assert root_key == "user"

    @pytest.mark.asyncio
    async def test_resolve_owner_root_propagates_unrelated_graphql_error(self):
        # A GraphQL error that is *not* the not-an-organization NOT_FOUND
        # must propagate rather than be silently downgraded to a user
        # fallback.
        from dependamerge.github_async import GraphQLError

        svc = GitHubService(token="test_token")

        async def fake_graphql(query, variables):
            raise GraphQLError(
                '[{"type": "FORBIDDEN", "message": "Resource not accessible"}]'
            )

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        with pytest.raises(GraphQLError):
            await svc._resolve_owner_root("someuser")
        await svc.close()

    @pytest.mark.asyncio
    async def test_resolve_owner_root_propagates_nested_not_found(self):
        # A NOT_FOUND on a *nested* field under the organization root
        # (path longer than ["organization"]) means the org resolved but a
        # sub-field failed; it must NOT be downgraded to a user fallback.
        # Only an exact top-level ["organization"] NOT_FOUND counts.
        from dependamerge.github_async import GraphQLError

        svc = GitHubService(token="test_token")

        async def fake_graphql(query, variables):
            raise GraphQLError(
                '[{"type": "NOT_FOUND", "path": ["organization", "repositories"], '
                '"message": "Could not resolve organization repositories."}]'
            )

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        with pytest.raises(GraphQLError):
            await svc._resolve_owner_root("someuser")
        await svc.close()

    @pytest.mark.asyncio
    async def test_resolve_owner_root_cached(self):
        svc = GitHubService(token="test_token")
        calls = 0

        async def fake_graphql(query, variables):
            nonlocal calls
            calls += 1
            return {"organization": {"repositories": {"nodes": []}}}

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        await svc._resolve_owner_root("acme")
        await svc._resolve_owner_root("acme")
        await svc.close()
        # Second call served from cache; only one probe.
        assert calls == 1

    @pytest.mark.asyncio
    async def test_fetch_repo_open_prs_reports_result_count_to_progress(self):
        # complete_repository must receive the in-scope PR count (len of
        # results), consistent with find_similar_prs / fetch_owner_open_prs,
        # not a hard-coded 0.
        completed: list[int] = []

        class _Progress:
            def start_repository(self, name):  # noqa: D401
                pass

            def update_operation(self, msg):
                pass

            def complete_repository(self, count):
                completed.append(count)

        svc = GitHubService(token="test_token")
        svc._progress = _Progress()  # type: ignore[assignment]

        async def fake_collect(owner, repo, *, only_automation=True):
            return [
                _make_pr(1, repo="acme/repo"),
                _make_pr(2, repo="acme/repo"),
            ]

        svc._collect_repo_open_prs = fake_collect  # type: ignore[assignment]

        results = await svc.fetch_repo_open_prs("acme", "repo")
        await svc.close()

        assert len(results) == 2
        assert completed == [2]

    @pytest.mark.asyncio
    async def test_iter_owner_repositories_excludes_archived_and_forks(self):
        svc = GitHubService(token="test_token")

        page = {
            "organization": {
                "repositories": {
                    "totalCount": 4,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "nameWithOwner": "acme/keep",
                            "isArchived": False,
                            "isFork": False,
                        },
                        {
                            "nameWithOwner": "acme/arch",
                            "isArchived": True,
                            "isFork": False,
                        },
                        {
                            "nameWithOwner": "acme/fork",
                            "isArchived": False,
                            "isFork": True,
                        },
                        {
                            "nameWithOwner": "acme/keep2",
                            "isArchived": False,
                            "isFork": False,
                        },
                    ],
                }
            }
        }

        async def fake_graphql(query, variables):
            return page

        svc._api.graphql = fake_graphql  # type: ignore[assignment]

        names = []
        async for repo in svc._iter_owner_repositories("acme"):
            names.append(repo["nameWithOwner"])
        await svc.close()

        assert names == ["acme/keep", "acme/keep2"]
