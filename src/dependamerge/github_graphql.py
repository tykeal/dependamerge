# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
GraphQL query strings for retrieving repositories in an organization and their
open pull requests, including status check rollups and basic file/comment data.

These queries are designed to batch-read as much as possible to reduce the
number of HTTP round-trips compared to multiple REST calls per PR.

Notes:
- The mergeable field is an enum: MERGEABLE | CONFLICTING | UNKNOWN
- The mergeStateStatus field includes states like CLEAN, DIRTY, BLOCKED, BEHIND, DRAFT, UNKNOWN
- statusCheckRollup provides both CheckRun and StatusContext results for the latest commit
"""

__all__ = [
    "ORG_REPOS_ONLY",
    "ORG_REPOS_WITH_OPEN_PRS",
    "REPO_OPEN_PRS_PAGE",
    "ENABLE_AUTO_MERGE",
    "GET_BRANCH_PROTECTION",
]

# Lightweight query to list repositories without PR nodes for accurate counting.
# totalCount is provided by the GitHub GraphQL API for free on connection
# objects, so the first page immediately reveals the org-wide repo total
# without requiring a separate counting pass.
ORG_REPOS_ONLY = """
query($org: String!, $reposCursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $reposCursor, orderBy: { field: NAME, direction: ASC }) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isArchived
      }
    }
  }
}
"""

# Fetch organization repositories with a first page of their open PRs.
# Use the returned pageInfo to continue paging repositories.
# Each repository node also includes pageInfo for its pull requests; for repos
# with more than 50 open PRs, use REPO_OPEN_PRS_PAGE to paginate further.
ORG_REPOS_WITH_OPEN_PRS = """
query($org: String!, $reposCursor: String) {
  organization(login: $org) {
    repositories(first: 30, after: $reposCursor, orderBy: { field: NAME, direction: ASC }) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isArchived
        pullRequests(
          states: OPEN
          first: 30
          orderBy: { field: CREATED_AT, direction: DESC }
        ) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            number
            title
            body
            url
            isDraft
            author { login }
            mergeable
            mergeStateStatus
            baseRefName
            headRefName
            headRefOid
            headRepository { nameWithOwner url isFork }
            baseRepository { nameWithOwner url }
            createdAt
            updatedAt
            files(first: 50) {
              nodes {
                path
                additions
                deletions
              }
            }
            comments(first: 10, orderBy: { field: UPDATED_AT, direction: DESC }) {
              nodes {
                author { login }
                body
                createdAt
              }
            }
            reviews(first: 20, states: [PENDING, COMMENTED, APPROVED, CHANGES_REQUESTED]) {
              nodes {
                id
                author { login }
                state
                body
                createdAt
                updatedAt
              }
            }

            commits(last: 1) {
              nodes {
                commit {
                  oid
                  statusCheckRollup {
                    state
                    contexts(first: 20) {
                      nodes {
                        __typename
                        ... on CheckRun {
                          name
                          status
                          conclusion
                        }
                        ... on StatusContext {
                          context
                          state
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Paginate open PRs for a specific repository when there are more than 50.
# Provide the repository owner/name and the PR cursor returned by previous pages.
REPO_OPEN_PRS_PAGE = """
query($owner: String!, $name: String!, $prsCursor: String, $prsPageSize: Int!, $filesPageSize: Int!, $commentsPageSize: Int!, $contextsPageSize: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(
      states: OPEN
      first: $prsPageSize
      after: $prsCursor
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        number
        title
        body
        url
        isDraft
        author { login }
        mergeable
        mergeStateStatus
        baseRefName
        headRefName
        headRefOid
        headRepository { nameWithOwner url isFork }
        baseRepository { nameWithOwner url }
        createdAt
        updatedAt
        files(first: $filesPageSize) {
          nodes {
            path
            additions
            deletions
          }
        }
        comments(first: $commentsPageSize, orderBy: { field: UPDATED_AT, direction: DESC }) {
          nodes {
            author { login }
            body
            createdAt
          }
        }
        reviews(first: 20, states: [PENDING, COMMENTED, APPROVED, CHANGES_REQUESTED]) {
          nodes {
            id
            author { login }
            state
            body
            createdAt
            updatedAt
          }
        }

        commits(last: 1) {
          nodes {
            commit {
              oid
              statusCheckRollup {
                state
                contexts(first: $contextsPageSize) {
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      status
                      conclusion
                    }
                    ... on StatusContext {
                      context
                      state
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# GraphQL mutations for managing review comments and reviews
DISMISS_REVIEW_COMMENT = """
mutation DismissReviewComment($commentId: ID!) {
  dismissPullRequestReviewComment(input: {
    pullRequestReviewCommentId: $commentId
  }) {
    pullRequestReviewComment {
      id
      state
      author { login }
    }
  }
}
"""

# GraphQL mutation to resolve a review thread
RESOLVE_REVIEW_THREAD = """
mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""

# GraphQL query to get review threads for a pull request
GET_PR_REVIEW_THREADS = """
query GetPullRequestReviewThreads($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          line
          originalLine
          diffSide
          startLine
          originalStartLine
          path
          comments(first: 10) {
            nodes {
              id
              author {
                login
              }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""

DISMISS_PULL_REQUEST_REVIEW = """
mutation DismissPullRequestReview($reviewId: ID!, $message: String!) {
  dismissPullRequestReview(input: {
    pullRequestReviewId: $reviewId
    message: $message
  }) {
    pullRequestReview {
      id
      state
      author { login }
    }
  }
}
"""

# GraphQL mutation to enable auto-merge on a pull request
ENABLE_AUTO_MERGE = """
mutation EnableAutoMerge($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod) {
  enablePullRequestAutoMerge(input: {
    pullRequestId: $pullRequestId
    mergeMethod: $mergeMethod
  }) {
    pullRequest {
      autoMergeRequest {
        enabledAt
        enabledBy { login }
        mergeMethod
      }
    }
  }
}
"""

# GraphQL query to get branch protection settings for a repository
GET_BRANCH_PROTECTION = """
query GetBranchProtection($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeCommitAllowed
    squashMergeAllowed
    rebaseMergeAllowed
    ref(qualifiedName: $branch) {
      branchProtectionRule {
        requiresLinearHistory
        requiresCommitSignatures
        requiredStatusCheckContexts
        requiresStatusChecks
        requiresApprovingReviews
        requiredApprovingReviewCount
        dismissesStaleReviews
        requiresCodeOwnerReviews
        restrictsPushes
        restrictsReviewDismissals
      }
    }
  }
}
"""
