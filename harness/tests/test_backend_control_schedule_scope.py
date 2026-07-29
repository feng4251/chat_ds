import unittest
from unittest.mock import AsyncMock, patch

from tools import backend_control


class BackendControlScheduleScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_fork_proves_already_held_source_turn(self):
        calls = AsyncMock(return_value='{"ok": true}')
        with patch.object(backend_control, "_request", new=calls):
            await backend_control.sessions_fork(
                title="Fork",
                user_id="user-1",
                session_id="session-1",
            )
        self.assertEqual(
            {
                "user_id": "user-1",
                "source_session_id": "session-1",
            },
            calls.await_args.kwargs["params"],
        )

    async def test_every_cron_mutation_proves_its_source_session(self):
        calls = AsyncMock(return_value='{"ok": true}')
        common = {
            "user_id": "user-1",
            "session_id": "session-1",
        }
        with patch.object(backend_control, "_request", new=calls):
            await backend_control.cronjob(
                "create",
                name="job",
                prompt="work",
                schedule="in 1h",
                **common,
            )
            await backend_control.cronjob(
                "update",
                job_id="job-1",
                name="updated",
                **common,
            )
            await backend_control.cronjob(
                "pause",
                job_id="job-1",
                **common,
            )
            await backend_control.cronjob(
                "resume",
                job_id="job-1",
                **common,
            )
            await backend_control.cronjob(
                "trigger",
                job_id="job-1",
                **common,
            )
            await backend_control.cronjob(
                "remove",
                job_id="job-1",
                **common,
            )

        self.assertEqual(6, calls.await_count)
        for call in calls.await_args_list:
            self.assertEqual(
                {
                    "user_id": "user-1",
                    "source_session_id": "session-1",
                },
                call.kwargs["params"],
            )

    async def test_cron_list_remains_read_only_and_session_scoped(self):
        calls = AsyncMock(return_value='{"ok": true}')
        with patch.object(backend_control, "_request", new=calls):
            await backend_control.cronjob(
                "list",
                user_id="user-1",
                session_id="session-1",
            )
        self.assertEqual(
            {
                "user_id": "user-1",
                "conversation_id": "session-1",
            },
            calls.await_args.kwargs["params"],
        )


if __name__ == "__main__":
    unittest.main()
