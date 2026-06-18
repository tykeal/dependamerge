# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""Tests for the shared gitreview module.

Covers:
- GitReviewInfo data model
- parse_gitreview() — pure text parser
- parse_gitreview_text() — backward-compatible alias
- derive_base_path() — static known-host lookup
- fetch_gitreview_from_github() — async GitHub API fetcher
- Backward-compatible re-exports from github2gerrit_detector
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from dependamerge.gitreview import (
    DEFAULT_GERRIT_PORT,
    GitReviewInfo,
    derive_base_path,
    fetch_gitreview_from_github,
    parse_gitreview,
    parse_gitreview_text,
)

# -----------------------------------------------------------------------
# Fixtures / constants
# -----------------------------------------------------------------------

TYPICAL_GITREVIEW = (
    "[gerrit]\n"
    "host=gerrit.linuxfoundation.org\n"
    "port=29418\n"
    "project=releng/lftools.git\n"
)

MINIMAL_GITREVIEW = "[gerrit]\nhost=gerrit.example.org\n"

SPACES_AROUND_EQUALS = (
    "[gerrit]\nhost = git.opendaylight.org\nport = 29418\nproject = aaa.git\n"
)

MIXED_CASE_KEYS = (
    "[gerrit]\nHost=gerrit.example.org\nPort=29419\nProject=apps/widgets.git\n"
)

NO_HOST = "[gerrit]\nport=29418\nproject=foo.git\n"

EMPTY_HOST = "[gerrit]\nhost=\nport=29418\nproject=foo.git\n"

NO_PORT = "[gerrit]\nhost=gerrit.example.org\nproject=foo.git\n"

NO_PROJECT = "[gerrit]\nhost=gerrit.example.org\nport=29418\n"

WHITESPACE_HOST = "[gerrit]\nhost=  gerrit.example.org  \n"

NON_DEFAULT_PORT = (
    "[gerrit]\nhost=gerrit.acme.org\nport=29419\nproject=acme/widgets.git\n"
)


# -----------------------------------------------------------------------
# GitReviewInfo data model
# -----------------------------------------------------------------------


class TestGitReviewInfoModel:
    """Tests for the GitReviewInfo frozen dataclass."""

    def test_default_values(self) -> None:
        info = GitReviewInfo(host="h")
        assert info.host == "h"
        assert info.port == DEFAULT_GERRIT_PORT
        assert info.project == ""
        assert info.base_path is None

    def test_is_valid_with_host(self) -> None:
        assert GitReviewInfo(host="h").is_valid is True

    def test_is_valid_empty_host(self) -> None:
        assert GitReviewInfo(host="").is_valid is False

    def test_frozen(self) -> None:
        info = GitReviewInfo(host="h")
        with pytest.raises(AttributeError):
            info.host = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = GitReviewInfo(host="h", port=1, project="p", base_path="bp")
        b = GitReviewInfo(host="h", port=1, project="p", base_path="bp")
        assert a == b

    def test_inequality_on_base_path(self) -> None:
        a = GitReviewInfo(host="h", base_path=None)
        b = GitReviewInfo(host="h", base_path="infra")
        assert a != b

    def test_hashable(self) -> None:
        """Frozen dataclasses should be hashable for use in sets/dicts."""
        info = GitReviewInfo(host="h", port=1, project="p")
        assert hash(info) is not None
        s = {info}
        assert info in s

    def test_default_port_constant(self) -> None:
        assert DEFAULT_GERRIT_PORT == 29418


# -----------------------------------------------------------------------
# derive_base_path — static known-host lookup
# -----------------------------------------------------------------------


class TestDeriveBasePath:
    def test_known_host(self) -> None:
        assert derive_base_path("gerrit.linuxfoundation.org") == "infra"

    def test_known_host_case_insensitive(self) -> None:
        assert derive_base_path("Gerrit.LinuxFoundation.Org") == "infra"

    def test_known_host_with_whitespace(self) -> None:
        assert derive_base_path("  gerrit.linuxfoundation.org  ") == "infra"

    def test_unknown_host(self) -> None:
        assert derive_base_path("gerrit.example.org") is None

    def test_empty_host(self) -> None:
        assert derive_base_path("") is None


# -----------------------------------------------------------------------
# parse_gitreview — pure parser
# -----------------------------------------------------------------------


class TestParseGitreview:
    def test_typical(self) -> None:
        info = parse_gitreview(TYPICAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"
        assert info.port == 29418
        assert info.project == "releng/lftools"
        assert info.base_path == "infra"

    def test_minimal_host_only(self) -> None:
        info = parse_gitreview(MINIMAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.port == DEFAULT_GERRIT_PORT
        assert info.project == ""
        assert info.base_path is None

    def test_spaces_around_equals(self) -> None:
        info = parse_gitreview(SPACES_AROUND_EQUALS)
        assert info is not None
        assert info.host == "git.opendaylight.org"
        assert info.port == 29418
        assert info.project == "aaa"

    def test_mixed_case_keys(self) -> None:
        info = parse_gitreview(MIXED_CASE_KEYS)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.port == 29419
        assert info.project == "apps/widgets"

    def test_no_host_returns_none(self) -> None:
        assert parse_gitreview(NO_HOST) is None

    def test_empty_host_returns_none(self) -> None:
        assert parse_gitreview(EMPTY_HOST) is None

    def test_no_port_defaults(self) -> None:
        info = parse_gitreview(NO_PORT)
        assert info is not None
        assert info.port == DEFAULT_GERRIT_PORT

    def test_no_project_ok(self) -> None:
        info = parse_gitreview(NO_PROJECT)
        assert info is not None
        assert info.project == ""

    def test_strips_whitespace_from_host(self) -> None:
        info = parse_gitreview(WHITESPACE_HOST)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_removes_dot_git_suffix(self) -> None:
        info = parse_gitreview(NON_DEFAULT_PORT)
        assert info is not None
        assert info.project == "acme/widgets"

    def test_non_default_port(self) -> None:
        info = parse_gitreview(NON_DEFAULT_PORT)
        assert info is not None
        assert info.port == 29419

    def test_empty_string(self) -> None:
        assert parse_gitreview("") is None

    def test_garbage_text(self) -> None:
        assert parse_gitreview("nothing useful here\n") is None

    def test_base_path_derived_for_lf_host(self) -> None:
        text = "[gerrit]\nhost=gerrit.linuxfoundation.org\nproject=foo.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path == "infra"

    def test_base_path_none_for_unknown_host(self) -> None:
        text = "[gerrit]\nhost=gerrit.acme.org\nproject=foo.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path is None

    def test_project_without_git_suffix(self) -> None:
        text = "[gerrit]\nhost=h\nproject=releng/builder\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.project == "releng/builder"

    def test_tabs_around_equals(self) -> None:
        text = "[gerrit]\nhost\t=\tgerrit.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_windows_line_endings(self) -> None:
        text = "[gerrit]\r\nhost=gerrit.example.org\r\nport=29418\r\n"
        info = parse_gitreview(text)
        assert info is not None
        # .strip() in the parser should handle trailing \r
        assert info.host == "gerrit.example.org"

    def test_trailing_whitespace(self) -> None:
        text = "[gerrit]\nhost=gerrit.example.org   \nport=29418\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_multiple_host_lines_takes_first(self) -> None:
        """If there are duplicate host= lines, take the first one."""
        text = "[gerrit]\nhost=first.example.org\nhost=second.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "first.example.org"

    def test_host_only_is_valid(self) -> None:
        text = "[gerrit]\nhost=gerrit.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.is_valid is True
        assert info.project == ""


# -----------------------------------------------------------------------
# parse_gitreview_text — backward-compatible alias
# -----------------------------------------------------------------------


class TestParseGitreviewTextAlias:
    """parse_gitreview_text must behave identically to parse_gitreview."""

    def test_alias_is_same_function(self) -> None:
        assert parse_gitreview_text is parse_gitreview

    def test_standard_gitreview(self) -> None:
        info = parse_gitreview_text(TYPICAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"
        assert info.port == 29418
        assert info.project == "releng/lftools"

    def test_no_host_returns_none(self) -> None:
        assert parse_gitreview_text(NO_HOST) is None

    def test_empty_string(self) -> None:
        assert parse_gitreview_text("") is None


# -----------------------------------------------------------------------
# fetch_gitreview_from_github — async GitHub API fetcher
# -----------------------------------------------------------------------


def _encode_gitreview(text: str) -> str:
    """Encode text as base64, matching the GitHub contents API format."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class TestFetchGitreviewFromGithub:
    def test_successful_fetch(self) -> None:
        encoded = _encode_gitreview(TYPICAL_GITREVIEW)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        result = asyncio.run(
            fetch_gitreview_from_github(mock_client, "lfit", "releng-test")
        )
        assert result is not None
        assert result.host == "gerrit.linuxfoundation.org"
        assert result.port == 29418
        assert result.project == "releng/lftools"
        assert result.base_path == "infra"

    def test_fetch_with_ref(self) -> None:
        encoded = _encode_gitreview(MINIMAL_GITREVIEW)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        result = asyncio.run(
            fetch_gitreview_from_github(mock_client, "org", "repo", ref="main")
        )
        assert result is not None
        assert result.host == "gerrit.example.org"

        # Verify the ref was included in the endpoint
        call_args = mock_client.get.call_args[0][0]
        assert "?ref=main" in call_args

    def test_file_not_found_returns_none(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("404 Not Found"))

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is None

    def test_non_dict_response_returns_none(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=[])

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is None

    def test_empty_content_returns_none(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": ""})

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is None

    def test_invalid_gitreview_content_returns_none(self) -> None:
        encoded = _encode_gitreview("no host here\n")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is None

    def test_api_error_returns_none(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("Server error"))

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is None

    def test_endpoint_without_ref(self) -> None:
        encoded = _encode_gitreview(MINIMAL_GITREVIEW)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        asyncio.run(fetch_gitreview_from_github(mock_client, "myorg", "myrepo"))
        call_args = mock_client.get.call_args[0][0]
        assert call_args == "/repos/myorg/myrepo/contents/.gitreview"
        assert "?ref=" not in call_args

    def test_endpoint_with_ref(self) -> None:
        encoded = _encode_gitreview(MINIMAL_GITREVIEW)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        asyncio.run(
            fetch_gitreview_from_github(mock_client, "myorg", "myrepo", ref="develop")
        )
        call_args = mock_client.get.call_args[0][0]
        assert call_args == "/repos/myorg/myrepo/contents/.gitreview?ref=develop"

    def test_base_path_derived_from_known_host(self) -> None:
        """The LF host should get base_path='infra' automatically."""
        text = "[gerrit]\nhost=gerrit.linuxfoundation.org\nproject=test.git\n"
        encoded = _encode_gitreview(text)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "lfit", "test"))
        assert result is not None
        assert result.base_path == "infra"

    def test_unknown_host_no_base_path(self) -> None:
        text = "[gerrit]\nhost=gerrit.custom.org\nproject=my/project.git\n"
        encoded = _encode_gitreview(text)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"content": encoded})

        result = asyncio.run(fetch_gitreview_from_github(mock_client, "org", "repo"))
        assert result is not None
        assert result.base_path is None


# -----------------------------------------------------------------------
# Backward-compatible re-exports from github2gerrit_detector
# -----------------------------------------------------------------------


class TestBackwardCompatibleReExports:
    """Verify that importing from github2gerrit_detector still works."""

    def test_gitreview_info_import(self) -> None:
        from dependamerge.github2gerrit_detector import GitReviewInfo as DetectorGRI

        assert DetectorGRI is GitReviewInfo

    def test_parse_gitreview_text_import(self) -> None:
        from dependamerge.github2gerrit_detector import (
            parse_gitreview_text as detector_parse,
        )

        assert detector_parse is parse_gitreview_text

    def test_fetch_gitreview_from_github_import(self) -> None:
        from dependamerge.github2gerrit_detector import (
            fetch_gitreview_from_github as detector_fetch,
        )

        assert detector_fetch is fetch_gitreview_from_github

    def test_detector_parse_works(self) -> None:
        from dependamerge.github2gerrit_detector import parse_gitreview_text as p

        info = p(TYPICAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"

    def test_detector_gitreview_info_construct(self) -> None:
        from dependamerge.github2gerrit_detector import GitReviewInfo as GRI

        info = GRI(host="h", port=29418, project="p", base_path="bp")
        assert info.host == "h"
        assert info.base_path == "bp"
        assert info.is_valid is True


# -----------------------------------------------------------------------
# Edge cases and regression tests
# -----------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and regressions from the original implementations."""

    def test_whitespace_stripped_from_all_fields(self) -> None:
        text = "[gerrit]\nhost= gerrit.example.org \nproject= my/project.git \n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.project == "my/project"

    def test_git_suffix_stripped(self) -> None:
        text = "[gerrit]\nhost=gerrit.example.org\nproject=my/project.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.project == "my/project"

    def test_no_git_suffix(self) -> None:
        text = "[gerrit]\nhost=gerrit.example.org\nproject=my/project\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.project == "my/project"

    def test_lf_host_has_infra_base_path(self) -> None:
        text = "[gerrit]\nhost=gerrit.linuxfoundation.org\nproject=test.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path == "infra"

    def test_unknown_host_no_base_path(self) -> None:
        text = "[gerrit]\nhost=gerrit.custom.org\nproject=my/project.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path is None

    def test_empty_host_returns_none(self) -> None:
        text = "[gerrit]\nhost=\nport=29418\nproject=my/project.git\n"
        assert parse_gitreview(text) is None

    def test_no_host_returns_none(self) -> None:
        text = "[gerrit]\nport=29418\nproject=my/project.git\n"
        assert parse_gitreview(text) is None

    def test_default_port_value(self) -> None:
        info = GitReviewInfo(host="gerrit.example.org")
        assert info.port == 29418

    def test_frozen_cannot_mutate(self) -> None:
        info = GitReviewInfo(host="gerrit.example.org")
        with pytest.raises(AttributeError):
            info.host = "other"  # type: ignore[misc]

    def test_is_valid_true(self) -> None:
        info = GitReviewInfo(
            host="gerrit.example.org", port=29418, project="releng/tool"
        )
        assert info.is_valid is True

    def test_is_valid_false(self) -> None:
        info = GitReviewInfo(host="", port=29418, project="releng/tool")
        assert info.is_valid is False

    def test_parse_captures_inline_comments_as_value(self) -> None:
        """Inline comments are NOT supported for ``.gitreview``.

        The parser captures the full line after ``=`` (minus surrounding
        whitespace), so a trailing ``# comment`` becomes part of the
        value. This is intentional — see ``parse_gitreview``'s docstring
        for the rationale (``.gitreview`` is not a commented INI dialect).
        """
        text = "[gerrit]\nhost=gerrit.example.org # primary\nport=29418\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org # primary"

    def test_inline_comment_on_port_falls_back_to_default(self) -> None:
        """A trailing comment makes the consequence of no comment support
        visible: because ``port=`` matches digits only, a commented port
        line fails to match and the parser falls back to the default port
        rather than raising. This is the same lack of comment support as
        ``test_parse_captures_inline_comments_as_value``, surfaced on a
        field whose stricter pattern rejects the malformed value.
        """
        text = "[gerrit]\nhost=gerrit.example.org\nport=29418 # primary\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"
        # The commented port line does not match ``\\d+$`` and is ignored.
        assert info.port == DEFAULT_GERRIT_PORT
