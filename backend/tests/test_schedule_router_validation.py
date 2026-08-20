import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from models import ScheduledJob
from routers.schedule_router import (
    _apply_job_update,
    _validate_platform_capabilities,
)
from schemas import ScheduledJobUpdate


class _CommitRecorder:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


class ScheduleRouterValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_native_tool_names_are_not_platform_schedule_capabilities(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_platform_capabilities(["write", "delegate_task"])
        self.assertEqual(raised.exception.status_code, 400)

    @staticmethod
    def _job() -> ScheduledJob:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return ScheduledJob(
            id="generic-schedule-row",
            user_id="generic-owner",
            conversation_id=None,
            name="Renamed warehouse monitor",
            prompt="Report the selected warehouse reading.",
            schedule_kind="interval",
            schedule_value="600",
            timezone="UTC",
            model_id=None,
            enabled_tools=None,
            enabled=True,
            delete_after_run=False,
            max_runs=5,
            run_count=0,
            expires_at=now + timedelta(hours=1),
            next_run_at=now + timedelta(minutes=10),
            last_run_at=None,
            last_status=None,
            consecutive_errors=0,
            created_at=now,
        )

    async def test_expiry_update_rejects_an_impossible_occurrence(self):
        db = _CommitRecorder()
        with self.assertRaises(HTTPException) as raised:
            await _apply_job_update(
                self._job(),
                ScheduledJobUpdate(
                    expires_at=datetime.now().astimezone()
                    - timedelta(minutes=1),
                ),
                "generic-owner",
                db,
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(db.commits, 0)

    async def test_disabling_an_already_expired_job_remains_available(self):
        job = self._job()
        job.expires_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1)
        )
        job.next_run_at = None
        db = _CommitRecorder()
        result = await _apply_job_update(
            job,
            ScheduledJobUpdate(enabled=False),
            "generic-owner",
            db,
        )
        self.assertFalse(result["enabled"])
        self.assertEqual(db.commits, 1)


if __name__ == "__main__":
    unittest.main()
