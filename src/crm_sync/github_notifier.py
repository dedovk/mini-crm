from __future__ import annotations

import logging
import os
from typing import Any

import requests

from crm_sync.services import SyncResult

LOGGER = logging.getLogger(__name__)
ALERT_TITLE = "[CRM] Синхронізація не працює 3 запуски поспіль"


class GitHubIssueNotifier:
    def __init__(self, *, token: str, repository: str, http: Any = requests) -> None:
        self.token = token
        self.repository = repository
        self.http = http
        self.api_url = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def notify(self, result: SyncResult) -> bool:
        if not self.token or not self.repository:
            return False
        if result.health.alert_due:
            existing = self._find_open_issue()
            if existing:
                return True
            components = ", ".join(result.health.failed_components) or "невідомо"
            body = (
                "Автоматична синхронізація CRM завершилася з помилками "
                f"{result.health.consecutive_failures} рази поспіль.\n\n"
                f"Проблемні компоненти: **{components}**.\n\n"
                "Перевірте останній GitHub Actions run та його Marketplace CRM summary. "
                "Нові Issue для наступних однакових помилок створюватися не будуть."
            )
            response = self.http.post(
                f"{self.api_url}/issues",
                headers=self.headers,
                json={"title": ALERT_TITLE, "body": body},
                timeout=30,
            )
            response.raise_for_status()
            return True
        if result.health.recovered:
            issue = self._find_open_issue()
            if not issue:
                return True
            number = issue["number"]
            comment = self.http.post(
                f"{self.api_url}/issues/{number}/comments",
                headers=self.headers,
                json={"body": "✅ Синхронізація відновилася. Лічильник послідовних помилок скинуто."},
                timeout=30,
            )
            comment.raise_for_status()
            closed = self.http.patch(
                f"{self.api_url}/issues/{number}",
                headers=self.headers,
                json={"state": "closed"},
                timeout=30,
            )
            closed.raise_for_status()
            return True
        return False

    def _find_open_issue(self) -> dict[str, Any] | None:
        response = self.http.get(
            f"{self.api_url}/issues",
            headers=self.headers,
            params={"state": "open", "per_page": 100},
            timeout=30,
        )
        response.raise_for_status()
        for issue in response.json():
            if issue.get("title") == ALERT_TITLE and "pull_request" not in issue:
                return issue
        return None


def notify_sync_health(result: SyncResult) -> bool:
    notifier = GitHubIssueNotifier(
        token=os.environ.get("GITHUB_TOKEN", "").strip(),
        repository=os.environ.get("GITHUB_REPOSITORY", "").strip(),
    )
    try:
        return notifier.notify(result)
    except Exception:
        LOGGER.exception("GitHub health notification failed")
        return False
