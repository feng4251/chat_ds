import json
import unittest

from tools.context import ToolContext
from tools.registry import ToolRegistry


class ToolContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_context_overrides_model_arguments(self):
        registry = ToolRegistry()

        async def handler(value: str, user_id: str, session_id: str) -> str:
            return json.dumps({
                "value": value,
                "user_id": user_id,
                "session_id": session_id,
            })

        registry.register(
            name="scoped",
            toolset="test",
            schema={"description": "", "parameters": {"type": "object"}},
            handler=handler,
        )
        result = json.loads(await registry.dispatch(
            "scoped",
            {
                "value": "ok",
                "user_id": "attacker",
                "session_id": "other-session",
            },
            context=ToolContext(user_id="user-a", session_id="session-a"),
        ))
        self.assertEqual(result["user_id"], "user-a")
        self.assertEqual(result["session_id"], "session-a")


if __name__ == "__main__":
    unittest.main()
