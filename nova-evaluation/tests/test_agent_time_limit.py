"""Nova's reported time budget must match Harbor's enforced agent timeout."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepagents_harbor.novacode_wrapper import _agent_time_limit_min


class AgentTimeLimitTest(unittest.TestCase):
    def test_agent_multiplier_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            (task / "task.toml").write_text("[agent]\ntimeout_sec = 3600\n")
            environment = SimpleNamespace(environment_dir=task / "environment")

            self.assertEqual(
                _agent_time_limit_min(environment, {"agent_timeout_multiplier": 0.5}), 30
            )
            self.assertEqual(_agent_time_limit_min(environment, {"timeout_multiplier": 0.25}), 15)
            self.assertEqual(
                _agent_time_limit_min(
                    environment,
                    {
                        "agent": {"override_timeout_sec": 1200, "max_timeout_sec": 900},
                        "agent_timeout_multiplier": 0.5,
                    },
                ),
                7.5,
            )

    def test_missing_task_limit_is_unknown(self):
        environment = SimpleNamespace(environment_dir=Path("missing-task/environment"))
        self.assertIsNone(_agent_time_limit_min(environment, {}))


if __name__ == "__main__":
    unittest.main()
