from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class IssueSinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedIssue:
    external_id: str
    external_url: str
    raw: dict[str, Any]


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    max_attempts: int = 5,
) -> httpx.Response:
    for attempt in range(max_attempts):
        resp = client.request(method, url, headers=headers, json=json)
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt >= max_attempts - 1:
            return resp
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = float(retry_after)
        else:
            sleep_s = min(8.0, 0.5 * (2**attempt))
        time.sleep(sleep_s)
    raise RuntimeError("unreachable retry loop")


class LinearClient:
    """Minimal Linear GraphQL client for creating and transitioning issues."""

    DEFAULT_ENDPOINT = "https://api.linear.app/graphql"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Linear API key is required")
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "LinearClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = _request_with_retry(
            self._client,
            "POST",
            self.endpoint,
            headers=self._headers(),
            json={"query": query, "variables": variables},
        )
        if resp.status_code >= 400:
            raise IssueSinkError(
                f"Linear API HTTP {resp.status_code}: {_truncate(resp.text)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise IssueSinkError(f"Linear API returned non-JSON: {exc}") from exc
        if data.get("errors"):
            raise IssueSinkError(f"Linear API errors: {data['errors']}")
        if "data" not in data:
            raise IssueSinkError("Linear API response missing data field")
        return data["data"]

    def resolve_team_id(self, team_key: str) -> str:
        team_key = team_key.strip()
        if not team_key:
            raise ValueError("team_key must be non-empty")
        data = self._graphql(
            """
            query TeamByKey($key: String!) {
              teams(filter: { key: { eq: $key } }) {
                nodes { id key name }
              }
            }
            """,
            {"key": team_key},
        )
        nodes = (data.get("teams") or {}).get("nodes") or []
        if not nodes:
            raise IssueSinkError(f"Linear team not found by key: {team_key}")
        return str(nodes[0]["id"])

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
    ) -> CreatedIssue:
        if not team_id.strip():
            raise ValueError("team_id is required")
        input_obj: dict[str, Any] = {
            "teamId": team_id.strip(),
            "title": title,
            "description": description,
        }
        if labels:
            label_ids = self._resolve_label_ids(team_id=team_id, labels=labels)
            if label_ids:
                input_obj["labelIds"] = label_ids
        data = self._graphql(
            """
            mutation IssueCreate($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier url title }
              }
            }
            """,
            {"input": input_obj},
        )
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise IssueSinkError(f"Linear issueCreate failed: {result}")
        issue = result.get("issue") or {}
        identifier = str(issue.get("identifier") or issue.get("id") or "")
        url = str(issue.get("url") or "")
        if not identifier or not url:
            raise IssueSinkError(f"Linear issueCreate response missing fields: {issue}")
        return CreatedIssue(external_id=identifier, external_url=url, raw=issue)

    def get_issue_state(self, identifier: str) -> dict[str, Any]:
        """Look up an issue by identifier (e.g. 'ENG-42') or UUID and return
        its `state` block: {id, name, type}.  Raises IssueSinkError on lookup
        failure so callers can decide whether to skip vs abort."""
        identifier = identifier.strip()
        if not identifier:
            raise ValueError("identifier is required")
        # If the value looks like a UUID (no dash-prefix-letters style key),
        # use it directly.  Otherwise resolve via the identifier filter.
        if "-" in identifier and identifier.split("-", 1)[0].isalpha():
            data = self._graphql(
                """
                query IssueByIdentifier($id: String!) {
                  issues(filter: { number: { eq: 0 } }, first: 1) { nodes { id } }
                  issue(id: $id) { id identifier url state { id name type } }
                }
                """,
                {"id": identifier},
            )
        else:
            data = self._graphql(
                """
                query IssueByUuid($id: String!) {
                  issue(id: $id) { id identifier url state { id name type } }
                }
                """,
                {"id": identifier},
            )
        issue = data.get("issue") or {}
        if not issue:
            raise IssueSinkError(f"Linear issue not found: {identifier}")
        state = issue.get("state") or {}
        return {
            "id": str(state.get("id") or ""),
            "name": str(state.get("name") or ""),
            "type": str(state.get("type") or ""),
            "issue_url": str(issue.get("url") or ""),
        }

    def _resolve_label_ids(self, *, team_id: str, labels: list[str]) -> list[str]:
        wanted = {label.strip().lower() for label in labels if label.strip()}
        if not wanted:
            return []
        data = self._graphql(
            """
            query TeamLabels($teamId: String!) {
              team(id: $teamId) {
                labels(first: 250) { nodes { id name } }
              }
            }
            """,
            {"teamId": team_id},
        )
        nodes = ((data.get("team") or {}).get("labels") or {}).get("nodes") or []
        ids: list[str] = []
        for node in nodes:
            name = str(node.get("name") or "").strip().lower()
            if name in wanted:
                ids.append(str(node["id"]))
        return ids


class GitHubClient:
    """Minimal GitHub REST client for creating and transitioning issues."""

    DEFAULT_BASE_URL = "https://api.github.com"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("GitHub API key is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> CreatedIssue:
        owner, name = _parse_repo(repo)
        url = f"{self.base_url}/repos/{owner}/{name}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = [label for label in labels if label.strip()]
        resp = _request_with_retry(
            self._client,
            "POST",
            url,
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code >= 400:
            raise IssueSinkError(
                f"GitHub API HTTP {resp.status_code}: {_truncate(resp.text)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise IssueSinkError(f"GitHub API returned non-JSON: {exc}") from exc
        number = data.get("number")
        html_url = data.get("html_url") or ""
        if number is None or not html_url:
            raise IssueSinkError(f"GitHub create-issue response missing fields: {data}")
        external_id = f"{owner}/{name}#{number}"
        return CreatedIssue(external_id=external_id, external_url=html_url, raw=data)

    def get_issue_state(self, *, repo: str, number: int) -> dict[str, Any]:
        owner, name = _parse_repo(repo)
        url = f"{self.base_url}/repos/{owner}/{name}/issues/{int(number)}"
        resp = _request_with_retry(
            self._client,
            "GET",
            url,
            headers=self._headers(),
        )
        if resp.status_code == 404:
            raise IssueSinkError(f"GitHub issue not found: {repo}#{number}")
        if resp.status_code >= 400:
            raise IssueSinkError(
                f"GitHub API HTTP {resp.status_code}: {_truncate(resp.text)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise IssueSinkError(f"GitHub API returned non-JSON: {exc}") from exc
        return {
            "state": str(data.get("state") or ""),
            "state_reason": str(data.get("state_reason") or ""),
            "html_url": str(data.get("html_url") or ""),
        }

    def close_issue(
        self,
        *,
        repo: str,
        number: int,
        state_reason: str = "completed",
    ) -> None:
        owner, name = _parse_repo(repo)
        url = f"{self.base_url}/repos/{owner}/{name}/issues/{int(number)}"
        resp = _request_with_retry(
            self._client,
            "PATCH",
            url,
            headers=self._headers(),
            json={"state": "closed", "state_reason": state_reason},
        )
        if resp.status_code >= 400:
            raise IssueSinkError(
                f"GitHub API HTTP {resp.status_code}: {_truncate(resp.text)}"
            )


def _parse_repo(repo: str) -> tuple[str, str]:
    cleaned = (repo or "").strip().strip("/")
    if "/" not in cleaned:
        raise ValueError(f"GitHub repo must be in 'owner/name' format: {repo!r}")
    owner, _, name = cleaned.partition("/")
    if not owner or not name:
        raise ValueError(f"GitHub repo must be in 'owner/name' format: {repo!r}")
    return owner, name


def _truncate(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
