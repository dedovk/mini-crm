from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "cron.yml"


def test_runner_queue_timeout_is_longer_than_sync_step_timeout() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "    timeout-minutes: 60" in workflow
    synchronize_block = workflow.split("- name: Synchronize orders", 1)[1]
    assert "        timeout-minutes: 15" in synchronize_block
    assert "cancel-in-progress: false" in workflow
