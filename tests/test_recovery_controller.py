import unittest
from types import SimpleNamespace

from core.recovery_controller import RecoveryState


def outcome(tool, success, category="unknown", detail="failure"):
    return SimpleNamespace(
        tool=tool,
        success=success,
        failure_category=category,
        diagnostic_tail=detail,
        diagnostic_head=detail,
    )


class TestRecoveryController(unittest.TestCase):
    def test_invalid_argument_retry_requires_inspection(self):
        state = RecoveryState(original_goal="Fix the Jira skill")
        state.observe(outcome("run_command", False, "invalid_arguments", "AttributeError"), {"command": "bad"})
        self.assertEqual(
            state.gate("run_command", {"command": "bad"}),
            "recovery_no_progress: identical action already failed without new evidence",
        )
        state.observe(outcome("read_file", True), {"path": "skills/jira/main.py"})
        self.assertIsNone(state.gate("run_command", {"command": "bad"}))

    def test_diagnostics_do_not_consume_corrective_budget(self):
        state = RecoveryState(original_goal="inspect a task")
        state.observe(outcome("read_file", False, "unknown", "missing"), {"path": "x"})
        self.assertEqual(state.corrective_failures, 0)
        state.observe(outcome("run_command", False, "command_failure", "exit 1"), {"command": "pytest"})
        self.assertEqual(state.corrective_failures, 1)

    def test_catalog_is_exact_and_includes_diagnostics_and_repair_tools(self):
        state = RecoveryState(original_goal="repair a skill", failed_tool="run_command")
        state.observe(outcome("run_command", False, "local_skill_defect", "skill error"), {"command": "jira"})
        names = state.allowed_tool_names({"run_command", "read_file", "edit_file", "invented_tool"})
        self.assertEqual(names, {"run_command", "read_file", "edit_file"})


if __name__ == "__main__":
    unittest.main()
