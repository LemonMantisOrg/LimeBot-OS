import asyncio
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

from core.llm_client import ChatRequest, ProviderConfig


class TestWebConfig(unittest.TestCase):
    def _setup_page_config_keys(self):
        setup_page = Path("web/src/components/setup/SetupPage.tsx")
        source = setup_page.read_text(encoding="utf-8")
        match = re.search(r"type\s+SetupConfig\s*=\s*\{(?P<body>.*?)\};", source, re.S)
        self.assertIsNotNone(match, "SetupPage.tsx must declare type SetupConfig")
        keys = set()
        for line in match.group("body").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            key = line.split(":", 1)[0].strip()
            if key:
                keys.add(key)
        self.assertTrue(keys, "SetupConfig must expose at least one setup field")
        return keys

    def tearDown(self):
        import config as config_module

        config_module._cached_config = None

    def _load_config_with_env(self, env_updates):
        import config as config_module

        config_module._cached_config = None
        with patch.dict(os.environ, env_updates, clear=False):
            return config_module.load_config(force_reload=True)

    def _make_web_channel(self, model="openai/gpt-4o", base_url="https://api.example.test/v1"):
        try:
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model=model, base_url=base_url),
        )
        return WebChannel(config=config, bus=MessageBus())

    def test_web_config_exists_with_port(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

        cfg = self._load_config_with_env({"WEB_PORT": "8001", "PORT": ""})
        self.assertTrue(hasattr(cfg, "web"))
        self.assertTrue(hasattr(cfg.web, "port"))
        self.assertIsInstance(cfg.web.port, int)
        self.assertEqual(cfg.web.port, 8001)

    def test_web_allowed_origins_defaults_to_local_frontend(self):
        cfg = self._load_config_with_env(
            {
                "WEB_ALLOWED_ORIGINS": "",
                "FRONTEND_PORT": "",
                "VITE_DEV_SERVER_PORT": "",
            }
        )
        self.assertEqual(
            cfg.web.allowed_origins,
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        )

    def test_web_allowed_origins_honors_explicit_env_values(self):
        cfg = self._load_config_with_env(
            {
                "WEB_ALLOWED_ORIGINS": "http://localhost:3001, http://127.0.0.1:3001",
            }
        )
        self.assertEqual(
            cfg.web.allowed_origins,
            ["http://localhost:3001", "http://127.0.0.1:3001"],
        )

    def test_web_allowed_origins_replaces_wildcard_with_local_defaults(self):
        cfg = self._load_config_with_env(
            {
                "WEB_ALLOWED_ORIGINS": "*",
                "VITE_DEV_SERVER_PORT": "5179",
            }
        )
        self.assertEqual(
            cfg.web.allowed_origins,
            ["http://localhost:5179", "http://127.0.0.1:5179"],
        )

    def test_non_loopback_bind_requires_auth_or_private_proxy_contract(self):
        from channels.web import resolve_web_bind_host

        self.assertEqual(
            resolve_web_bind_host(
                "0.0.0.0", has_api_key=False, trusted_proxy_only=False
            ),
            "127.0.0.1",
        )
        self.assertEqual(
            resolve_web_bind_host(
                "0.0.0.0", has_api_key=True, trusted_proxy_only=False
            ),
            "0.0.0.0",
        )
        self.assertEqual(
            resolve_web_bind_host(
                "0.0.0.0", has_api_key=False, trusted_proxy_only=True
            ),
            "0.0.0.0",
        )
        self.assertEqual(
            resolve_web_bind_host(
                "127.0.0.1", has_api_key=False, trusted_proxy_only=False
            ),
            "127.0.0.1",
        )

    def test_persona_preview_uses_llm_client(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        provider = ProviderConfig(
            source_model="openai/gpt-4o",
            model="gpt-4o",
            base_url="https://api.example.test/v1",
            api_key="openai-secret",
            custom_llm_provider=None,
            is_codex=False,
        )
        fake_response = type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message", (), {"content": "Preview reply"}
                            )()
                        },
                    )()
                ]
            },
        )()
        channel.llm_client = SimpleNamespace(
            resolve_provider=MagicMock(return_value=provider),
            complete=AsyncMock(return_value=fake_response),
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "config.load_config", return_value=channel.config
        ), patch(
            "core.prompt.build_stable_system_prompt", return_value="system prompt"
        ), patch(
            "core.prompt.get_identity_data",
            return_value={"style": "Base style", "web_style": "Web style"},
        ), patch(
            "core.prompt.SOUL_FILE", Path(tmpdir) / "SOUL.md"
        ):
            client = TestClient(channel.app)
            response = client.post(
                "/api/persona/preview",
                json={
                    "persona": {"name": "Lime"},
                    "channel": "web",
                    "user_message": "Say hi",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["preview_text"], "Preview reply")
        self.assertEqual(payload["effective_style"], "Web style")
        channel.llm_client.resolve_provider.assert_called_once_with(
            "openai/gpt-4o", default_base_url="https://api.example.test/v1"
        )
        channel.llm_client.complete.assert_awaited_once()
        request = channel.llm_client.complete.await_args.args[1]
        self.assertIsInstance(request, ChatRequest)
        self.assertEqual(request.max_tokens, 180)
        self.assertEqual(request.session_id, "persona-preview")
        self.assertEqual(
            request.messages,
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "Say hi"},
            ],
        )

    def test_llm_health_uses_llm_client(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="openai-codex/gpt-5.4",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        provider = ProviderConfig(
            source_model="openai-codex/gpt-5.4",
            model="gpt-5.4",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="codex-secret",
            custom_llm_provider="openai",
            is_codex=True,
        )
        channel.llm_client = SimpleNamespace(
            resolve_provider=MagicMock(return_value=provider),
            complete=AsyncMock(return_value=object()),
        )

        with patch("config.load_config", return_value=channel.config):
            client = TestClient(channel.app)
            response = client.get("/api/llm/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "Healthy")
        self.assertEqual(payload["model"], "openai-codex/gpt-5.4")
        channel.llm_client.resolve_provider.assert_called_once_with(
            "openai-codex/gpt-5.4",
            default_base_url="https://chatgpt.com/backend-api/codex",
        )
        channel.llm_client.complete.assert_awaited_once()
        request = channel.llm_client.complete.await_args.args[1]
        self.assertIsInstance(request, ChatRequest)
        self.assertEqual(request.max_tokens, 16)
        self.assertEqual(request.session_id, "llm-health")
        self.assertEqual(request.messages, [{"role": "user", "content": "hi"}])

    def test_liveness_is_public_and_minimal(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        channel.config.whitelist.api_key = "private-app-key"
        response = TestClient(channel.app).get("/api/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "live")
        self.assertIn("version", response.json())
        self.assertIn("boot_id", response.json())
        self.assertNotIn("private-app-key", response.text)
        self.assertNotIn("model", response.json())

    def test_readiness_returns_503_until_agent_is_available(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        response = TestClient(channel.app).get("/api/ready")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ready"])
        self.assertEqual(response.json()["phase"], "agent")

    def test_degraded_readiness_is_healthy_and_redacted(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        channel.config.whitelist.api_key = "private-app-key"
        channel.agent = SimpleNamespace(
            get_readiness_status=lambda: {
                "status": "degraded",
                "phase": "degraded",
                "ready": True,
                "elapsed_ms": 12,
                "degraded_reasons": ["mcp_unavailable"],
                "failure_code": None,
            }
        )
        client = TestClient(channel.app)

        unauthorized = client.get("/api/ready")
        response = client.get(
            "/api/ready", headers={"X-API-Key": "private-app-key"}
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertEqual(response.json()["degraded_reasons"], ["mcp_unavailable"])
        self.assertNotIn("private-app-key", response.text)

    def test_setup_complete_validates_model_and_schedules_one_restart(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="ollama/llama3", base_url="http://127.0.0.1:11434/v1"
        )
        channel._persist_config_values = AsyncMock(return_value=channel.config)
        channel._probe_setup_llm = AsyncMock(return_value=17)

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "channels.web._SETUP_STATE_PATH", Path(tmpdir) / "setup-state.json"
        ), patch("channels.web._schedule_restart") as schedule_restart:
            client = TestClient(channel.app)
            response = client.post(
                "/api/setup/complete",
                json={
                    "env": {
                        "LLM_MODEL": "ollama/llama3",
                        "APP_API_KEY": "bootstrap-secret",
                        "ALLOWED_PATHS": ["./persona"],
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "restarting")
        self.assertEqual(payload["latency_ms"], 17)
        self.assertEqual(payload["boot_id"], channel._boot_id)
        self.assertTrue(payload["restart_token"])
        self.assertNotIn("bootstrap-secret", response.text)
        schedule_restart.assert_called_once_with()
        channel._probe_setup_llm.assert_awaited_once()

    def test_setup_complete_accepts_first_run_wizard_payload(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="gemini/gemini-2.0-flash", base_url=None
        )
        channel._persist_config_values = AsyncMock(return_value=channel.config)
        channel._probe_setup_llm = AsyncMock(return_value=23)

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "channels.web._SETUP_STATE_PATH", Path(tmpdir) / "setup-state.json"
        ), patch("channels.web._schedule_restart"):
            client = TestClient(channel.app)
            response = client.post(
                "/api/setup/complete",
                json={
                    "env": {
                        "LLM_MODEL": "gemini/gemini-2.0-flash",
                        "GEMINI_API_KEY": "gemini-secret",
                        "OPENROUTER_API_KEY": "",
                        "OPENAI_API_KEY": "",
                        "ANTHROPIC_API_KEY": "",
                        "XAI_API_KEY": "",
                        "DEEPSEEK_API_KEY": "",
                        "MOONSHOT_API_KEY": "",
                        "DASHSCOPE_API_KEY": "",
                        "NVIDIA_API_KEY": "",
                        "DISCORD_TOKEN": "",
                        "ENABLE_WHATSAPP": "false",
                        "WHATSAPP_BRIDGE_URL": "ws://localhost:3000",
                        "ALLOWED_PATHS": ["./persona", "./logs"],
                        "ENABLE_DYNAMIC_PERSONALITY": "false",
                        "APP_API_KEY": "bootstrap-secret",
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "restarting")
        self.assertEqual(response.json()["boot_id"], channel._boot_id)
        self.assertTrue(response.json()["restart_token"])
        channel._persist_config_values.assert_awaited_once()

    def test_setup_complete_allowlist_accepts_every_setup_wizard_field(self):
        from channels.web import _ALLOWED_SETUP_ENV_KEYS

        wizard_keys = self._setup_page_config_keys()
        unsupported_keys = wizard_keys - _ALLOWED_SETUP_ENV_KEYS

        self.assertFalse(
            unsupported_keys,
            "SetupPage posts fields that /api/setup/complete rejects: "
            f"{sorted(unsupported_keys)}",
        )

    def test_setup_complete_does_not_restart_when_llm_validation_fails(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="ollama/llama3", base_url="http://127.0.0.1:11434/v1"
        )
        channel._persist_config_values = AsyncMock(return_value=channel.config)
        channel._probe_setup_llm = AsyncMock(
            side_effect=RuntimeError("401 unauthorized: rejected credential")
        )

        with patch("channels.web._schedule_restart") as schedule_restart:
            client = TestClient(channel.app)
            response = client.post(
                "/api/setup/complete",
                json={
                    "env": {
                        "LLM_MODEL": "ollama/llama3",
                        "APP_API_KEY": "do-not-echo",
                    }
                },
            )

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["stage"], "llm_check")
        self.assertEqual(payload["code"], "invalid_credentials")
        self.assertTrue(payload["config_saved"])
        self.assertNotIn("do-not-echo", response.text)
        self.assertNotIn("rejected credential", response.text)
        schedule_restart.assert_not_called()
        channel._persist_config_values.assert_awaited_once_with(
            {
                "LLM_MODEL": "ollama/llama3",
                "APP_API_KEY": "do-not-echo",
            },
            activate_config=False,
        )

    def test_setup_complete_times_out_llm_validation_without_restarting(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="openai-codex/gpt-5.4", base_url=None
        )
        channel._persist_config_values = AsyncMock(return_value=channel.config)

        async def slow_probe():
            await asyncio.sleep(1)

        channel._probe_setup_llm = slow_probe

        with patch("channels.web._SETUP_LLM_PROBE_TIMEOUT_SECONDS", 0.01), patch(
            "channels.web._schedule_restart"
        ) as schedule_restart:
            client = TestClient(channel.app)
            response = client.post(
                "/api/setup/complete",
                json={
                    "env": {
                        "LLM_MODEL": "openai-codex/gpt-5.4",
                        "APP_API_KEY": "do-not-echo",
                    }
                },
            )

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["stage"], "llm_check")
        self.assertEqual(payload["code"], "provider_timeout")
        self.assertTrue(payload["config_saved"])
        self.assertTrue(payload["retryable"])
        self.assertNotIn("do-not-echo", response.text)
        schedule_restart.assert_not_called()

    def test_setup_complete_rejects_unknown_fields_before_persisting(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(model="ollama/llama3", base_url=None)
        channel._persist_config_values = AsyncMock(return_value=channel.config)
        client = TestClient(channel.app)
        response = client.post(
            "/api/setup/complete",
            json={
                "env": {
                    "LLM_MODEL": "ollama/llama3",
                    "APP_API_KEY": "secret",
                    "UNSUPPORTED_SETUP_VALUE": "nope",
                }
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "unknown_fields")
        channel._persist_config_values.assert_not_awaited()

    def test_setup_status_correlates_restart_without_echoing_token(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel(
            model="ollama/llama3", base_url="http://127.0.0.1:11434/v1"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "setup-state.json"
            state_path.write_text(
                '{"restart_token":"restart-secret"}', encoding="utf-8"
            )
            with patch("channels.web._SETUP_STATE_PATH", state_path), patch(
                "config.load_config", return_value=channel.config
            ), patch(
                "core.prompt.get_setup_state",
                return_value={"complete": False, "missing": ["SOUL.md"]},
            ):
                client = TestClient(channel.app)
                response = client.get(
                    "/api/setup/status",
                    params={"restart_token": "restart-secret"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["restart_recognized"])
        self.assertEqual(payload["boot_id"], channel._boot_id)
        self.assertNotIn("restart-secret", response.text)

    def test_configured_api_key_requires_auth_before_persona_is_complete(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        channel.config.whitelist.api_key = "app-secret"
        client = TestClient(channel.app)

        unauthorized = client.get("/api/config")
        authorized = client.get("/api/config", headers={"X-API-Key": "app-secret"})

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)

    def test_config_api_exposes_effective_approval_policy(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        loaded = self._load_config_with_env(
            {"APPROVAL_POLICY_PROFILE": "review", "AUTONOMOUS_MODE": "true"}
        )
        with patch("config.load_config", return_value=loaded):
            response = TestClient(channel.app).get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["env"]["APPROVAL_POLICY_PROFILE"], "review")
        self.assertEqual(response.json()["env"]["AUTONOMOUS_MODE"], "false")

    def test_config_api_serializes_moonshot_secret_from_alias_key(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        loaded = self._load_config_with_env({"KIMI_API_KEY": "moonshot-secret"})
        with patch.dict(
            os.environ,
            {"KIMI_API_KEY": "moonshot-secret"},
            clear=False,
        ), patch("config.load_config", return_value=loaded):
            response = TestClient(channel.app).get("/api/config")

        self.assertEqual(response.status_code, 200)
        secrets = response.json()["secrets"]
        self.assertTrue(secrets["MOONSHOT_API_KEY"]["configured"])
        self.assertEqual(secrets["MOONSHOT_API_KEY"]["last4"], "cret")

    def test_config_api_omits_legacy_search_api_secrets(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        env = {
            "TAVILY_API_KEY": "tvly-abcd",
            "BRAVE_API_KEY": "brave-wxyz",
            "SERPAPI_KEY": "serp-1234",
            "SEARCH_PROVIDER": "brave",
        }
        loaded = self._load_config_with_env(env)
        with patch.dict(os.environ, env, clear=False), patch(
            "config.load_config", return_value=loaded
        ):
            response = TestClient(channel.app).get("/api/config")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        secrets = payload["secrets"]
        self.assertNotIn("TAVILY_API_KEY", secrets)
        self.assertNotIn("BRAVE_SEARCH_API_KEY", secrets)
        self.assertNotIn("SERPAPI_API_KEY", secrets)
        self.assertNotIn("SEARCH_PROVIDER", payload["env"])

    def test_config_api_serializes_elevenlabs_secret(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("Missing web test dependencies.")

        channel = self._make_web_channel()
        loaded = self._load_config_with_env({"ELEVENLABS_API_KEY": "eleven-secret"})
        with patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "eleven-secret"}, clear=False
        ), patch("config.load_config", return_value=loaded):
            response = TestClient(channel.app).get("/api/config")

        self.assertEqual(response.status_code, 200)
        secrets = response.json()["secrets"]
        self.assertTrue(secrets["ELEVENLABS_API_KEY"]["configured"])
        self.assertEqual(secrets["ELEVENLABS_API_KEY"]["last4"], "cret")

    def test_merge_env_lines_preserves_comments_and_clears_secret(self):
        from channels.web import _merge_env_lines

        merged = _merge_env_lines(
            ["# keep this", "LLM_MODEL=old", "OPENAI_API_KEY=old", "OTHER=1"],
            {"LLM_MODEL": "openai/gpt-4o-mini"},
            {"OPENAI_API_KEY"},
        )

        self.assertEqual(
            merged,
            [
                "# keep this",
                "LLM_MODEL=openai/gpt-4o-mini",
                "OPENAI_API_KEY=",
                "OTHER=1",
            ],
        )

    def test_persist_config_values_writes_env_and_paths_atomically(self):
        channel = self._make_web_channel(model="ollama/llama3", base_url=None)

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "config.reload_config", return_value=channel.config
        ), patch.dict(os.environ, {}, clear=False):
            previous_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path(".env").write_text(
                    "# existing comment\nLLM_MODEL=old\nOTHER=keep\n",
                    encoding="utf-8",
                )
                asyncio.run(
                    channel._persist_config_values(
                        {
                            "LLM_MODEL": "ollama/llama3",
                            "APP_API_KEY": "new-app-key",
                            "ALLOWED_PATHS": ["./persona", "./logs"],
                        },
                        activate_config=False,
                    )
                )

                env_text = Path(".env").read_text(encoding="utf-8")
                self.assertIn("# existing comment", env_text)
                self.assertIn("LLM_MODEL=ollama/llama3", env_text)
                self.assertIn("OTHER=keep", env_text)
                self.assertIn("APP_API_KEY=new-app-key", env_text)
                self.assertEqual(
                    Path("allowed_paths.txt").read_text(encoding="utf-8"),
                    "./persona\n./logs",
                )
                self.assertEqual(list(Path(".").glob(".*.tmp")), [])
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
