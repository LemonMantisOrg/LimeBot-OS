import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

if "loguru" not in sys.modules:
    loguru = types.ModuleType("loguru")

    class _DummyLogger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    loguru.logger = _DummyLogger()
    sys.modules["loguru"] = loguru


class TestTaskWorkspace(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_persists_attempts_and_artifacts(self):
        from core.task_tracker import TaskStatus, TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            workspace = await tracker.create_workspace(
                "Implement durable workspace",
                "web",
                session_key="web_chat",
                chat_id="chat-1",
                metadata={"source": "test"},
            )

            updated = await tracker.update_workspace(
                workspace.workspace_id,
                status=TaskStatus.RUNNING.value,
                metadata_update={"ticket": "LIME-10"},
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, TaskStatus.RUNNING.value)
            self.assertEqual(updated.metadata["ticket"], "LIME-10")

            attempt = await tracker.add_workspace_attempt(
                workspace.workspace_id,
                model="openai/gpt-4o",
                summary="Initial pass",
                status=TaskStatus.RUNNING.value,
            )
            self.assertIsNotNone(attempt)

            finished_attempt = await tracker.complete_workspace_attempt(
                workspace.workspace_id,
                attempt.attempt_id,
            )
            self.assertEqual(finished_attempt.status, TaskStatus.COMPLETED.value)

            artifact = await tracker.add_workspace_artifact(
                workspace.workspace_id,
                kind="diff",
                title="Patch preview",
                path="scratch/patch.diff",
            )
            self.assertIsNotNone(artifact)

            await tracker.update_workspace(
                workspace.workspace_id,
                status=TaskStatus.COMPLETED.value,
            )
            tracker._flush_sync()

            reloaded = TaskTracker(data_dir=tmpdir)
            stored = await reloaded.get_workspace(workspace.workspace_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, TaskStatus.COMPLETED.value)
            self.assertEqual(len(stored.attempts), 1)
            self.assertEqual(len(stored.artifacts), 1)
            self.assertEqual(stored.artifacts[0].path, "scratch/patch.diff")
            self.assertEqual(stored.metadata["source"], "test")
            self.assertEqual(stored.metadata["ticket"], "LIME-10")

    async def test_create_workspace_is_visible_immediately_after_kill(self):
        from core.task_tracker import TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            workspace = await tracker.create_workspace(
                "Companion chat",
                "app",
                metadata={"source": "app"},
            )
            data_file = Path(tmpdir) / "tasks.json"
            self.assertTrue(data_file.exists())
            payload = data_file.read_text(encoding="utf-8")
            self.assertIn(workspace.workspace_id, payload)

            reloaded = TaskTracker(data_dir=tmpdir)
            stored = await reloaded.get_workspace(workspace.workspace_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.title, "Companion chat")

    async def test_workspace_listing_filters_terminal_items(self):
        from core.task_tracker import TaskStatus, TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            active = await tracker.create_workspace("Active task", "web")
            done = await tracker.create_workspace("Finished task", "ci")
            await tracker.update_workspace(
                done.workspace_id, status=TaskStatus.COMPLETED.value
            )

            active_only = await tracker.list_workspaces(active_only=True)
            self.assertEqual(
                [workspace.workspace_id for workspace in active_only],
                [active.workspace_id],
            )

            ci_only = await tracker.list_workspaces(origin_filter="ci")
            self.assertEqual(
                [workspace.workspace_id for workspace in ci_only],
                [done.workspace_id],
            )

    async def test_tracker_loads_legacy_file_without_workspaces(self):
        from core.task_tracker import TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tasks.json"
            path.write_text('{"active": [], "history": []}', encoding="utf-8")

            tracker = TaskTracker(data_dir=tmpdir)
            workspaces = await tracker.list_workspaces()
            self.assertEqual(workspaces, [])

    async def test_workspace_changeset_artifact_refreshes_without_replacing_identity(self):
        from core.task_tracker import TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            workspace = await tracker.create_workspace("Review patch", "app")
            artifact = await tracker.add_workspace_artifact(
                workspace.workspace_id,
                kind="change_set",
                title="Patch review",
                metadata={"status": "awaiting_approval", "summary": "One file"},
            )
            refreshed = await tracker.update_workspace_artifact(
                workspace.workspace_id,
                artifact.artifact_id,
                metadata_update={"status": "verified", "verification": [{"status": "passed"}]},
            )
            stored = await tracker.get_workspace(workspace.workspace_id)

        self.assertEqual(refreshed.artifact_id, artifact.artifact_id)
        self.assertEqual(stored.artifacts[0].metadata["status"], "verified")
        self.assertEqual(stored.artifacts[0].metadata["verification"][0]["status"], "passed")


class TestWorkspaceWebRoutes(unittest.TestCase):
    def _make_channel(self):
        try:
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(
                model="openai/gpt-4o", base_url="https://api.example.test/v1"
            ),
        )
        return WebChannel(config=config, bus=MessageBus())

    def test_workspace_routes_create_and_fetch_records(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        from core.task_tracker import TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            channel = self._make_channel()

            with patch("core.task_tracker.get_task_tracker", return_value=tracker):
                client = TestClient(channel.app)

                create_response = client.post(
                    "/api/workspaces",
                    json={
                        "title": "Codex parity workspace",
                        "origin": "web",
                        "session_key": "web_chat",
                        "chat_id": "chat-9",
                        "metadata": {"priority": "p1"},
                    },
                )
                self.assertEqual(create_response.status_code, 200)
                workspace = create_response.json()["workspace"]
                workspace_id = workspace["workspace_id"]
                self.assertEqual(workspace["title"], "Codex parity workspace")

                status_response = client.post(
                    f"/api/workspaces/{workspace_id}/status",
                    json={"status": "running", "metadata": {"owner": "lime"}},
                )
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(
                    status_response.json()["workspace"]["metadata"]["owner"], "lime"
                )

                artifact_response = client.post(
                    f"/api/workspaces/{workspace_id}/artifacts",
                    json={"kind": "log", "title": "Run log", "path": "logs/run.log"},
                )
                self.assertEqual(artifact_response.status_code, 200)
                self.assertEqual(artifact_response.json()["artifact"]["kind"], "log")

                list_response = client.get("/api/workspaces")
                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(len(list_response.json()["workspaces"]), 1)

                detail_response = client.get(f"/api/workspaces/{workspace_id}")
                self.assertEqual(detail_response.status_code, 200)
                detail = detail_response.json()["workspace"]
                self.assertEqual(detail["artifacts"][0]["path"], "logs/run.log")

    def test_workspace_routes_validate_required_fields(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        from core.task_tracker import TaskTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TaskTracker(data_dir=tmpdir)
            channel = self._make_channel()

            with patch("core.task_tracker.get_task_tracker", return_value=tracker):
                client = TestClient(channel.app)

                create_response = client.post("/api/workspaces", json={"origin": "web"})
                self.assertEqual(create_response.status_code, 400)

                artifact_response = client.post(
                    "/api/workspaces/missing/artifacts",
                    json={"kind": "diff"},
                )
                self.assertEqual(artifact_response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
