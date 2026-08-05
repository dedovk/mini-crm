from crm_sync.github_notifier import ALERT_TITLE, GitHubIssueNotifier
from crm_sync.models import SyncHealthState
from crm_sync.services import SyncResult


class Response:
    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class HttpStub:
    def __init__(self, issues=None) -> None:
        self.issues = issues or []
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response(self.issues)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response({"number": 10})

    def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return Response()


def result_with_health(health: SyncHealthState) -> SyncResult:
    return SyncResult(
        dry_run=False,
        source_orders={},
        failed_sources=health.failed_components,
        warnings=(),
        fetched_orders=0,
        new_orders=0,
        stale_orders=0,
        pending_shipments=0,
        shipment_statuses=0,
        health=health,
    )


def test_notifier_creates_one_issue_when_threshold_is_reached() -> None:
    http = HttpStub()
    notifier = GitHubIssueNotifier(token="token", repository="owner/repo", http=http)

    notified = notifier.notify(
        result_with_health(
            SyncHealthState(
                consecutive_failures=3,
                alert_due=True,
                failed_components=("prom",),
            )
        )
    )

    assert notified
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["json"]["title"] == ALERT_TITLE


def test_notifier_closes_existing_issue_after_recovery() -> None:
    http = HttpStub(issues=[{"number": 10, "title": ALERT_TITLE}])
    notifier = GitHubIssueNotifier(token="token", repository="owner/repo", http=http)

    notified = notifier.notify(result_with_health(SyncHealthState(recovered=True)))

    assert notified
    assert [call[0] for call in http.calls] == ["GET", "POST", "PATCH"]
    assert http.calls[-1][2]["json"] == {"state": "closed"}
