import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class TestWebModelRegistry(unittest.TestCase):
    def test_llm_models_expose_current_deepseek_v4_models_first(self):
        try:
            from fastapi.testclient import TestClient
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model="deepseek/deepseek-v4-flash", base_url=None),
        )
        channel = WebChannel(config=config, bus=MessageBus())

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}, clear=False), patch(
            "channels.web.get_codex_oauth_status",
            return_value={"configured": False, "provider": "openai-codex"},
        ):
            response = TestClient(channel.app).get("/api/llm/models")

        self.assertEqual(response.status_code, 200)
        deepseek_models = [
            model for model in response.json()["models"] if model["provider"] == "deepseek"
        ]
        self.assertEqual(
            [model["id"] for model in deepseek_models[:2]],
            ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"],
        )

    def test_llm_models_returns_current_openai_curated_models(self):
        try:
            from fastapi.testclient import TestClient
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model="openai/gpt-5.6-sol", base_url=None),
        )
        channel = WebChannel(config=config, bus=MessageBus())

        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False), patch(
            "channels.web.get_codex_oauth_status",
            return_value={"configured": False, "provider": "openai-codex"},
        ):
            response = TestClient(channel.app).get("/api/llm/models")

        self.assertEqual(response.status_code, 200)
        openai_models = [
            model for model in response.json()["models"] if model["provider"] == "openai"
        ]
        self.assertEqual(
            [model["id"] for model in openai_models],
            [
                "openai/gpt-5.6-sol",
                "openai/gpt-5.6-terra",
                "openai/gpt-5.6-luna",
                "openai/gpt-5.5",
                "openai/gpt-5.4",
            ],
        )

    def test_load_piai_provider_models_uses_registry_entries(self):
        try:
            from channels import web as web_module
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        registry = """
export const MODELS = {
  "openai-codex": {
    "gpt-5.6-sol": {
      id: "gpt-5.6-sol",
      name: "GPT-5.6 Sol",
      provider: "openai-codex",
    },
    "gpt-5.6-luna": {
      id: "gpt-5.6-luna",
      name: "GPT-5.6 Luna",
      provider: "openai-codex",
    },
    "gpt-5.6-terra": {
      id: "gpt-5.6-terra",
      name: "GPT-5.6 Terra",
      provider: "openai-codex",
    },
    "gpt-5.3-codex": {
      id: "gpt-5.3-codex",
      name: "GPT-5.3 Codex",
      provider: "openai-codex",
    },
    "gpt-5.4": {
      id: "gpt-5.4",
      name: "GPT-5.4",
      provider: "openai-codex",
    },
    "gpt-5.4-mini": {
      id: "gpt-5.4-mini",
      name: "GPT-5.4 Mini",
      provider: "openai-codex",
    }
  },
  "openai": {}
}
"""

        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "models.generated.js"
            registry_path.write_text(registry, encoding="utf-8")

            original_path = web_module._PIAI_MODELS_JS_PATH
            original_cache = dict(web_module._PIAI_PROVIDER_MODEL_CACHE)
            try:
                web_module._PIAI_MODELS_JS_PATH = registry_path
                web_module._PIAI_PROVIDER_MODEL_CACHE.clear()
                models = web_module._filter_supported_codex_models(
                    web_module._load_piai_provider_models("openai-codex")
                )
            finally:
                web_module._PIAI_MODELS_JS_PATH = original_path
                web_module._PIAI_PROVIDER_MODEL_CACHE.clear()
                web_module._PIAI_PROVIDER_MODEL_CACHE.update(original_cache)

        self.assertEqual(
            models,
            [
                {
                    "id": "openai-codex/gpt-5.6-sol",
                    "name": "GPT-5.6 Sol",
                    "provider": "openai-codex",
                },
                {
                    "id": "openai-codex/gpt-5.6-luna",
                    "name": "GPT-5.6 Luna",
                    "provider": "openai-codex",
                },
                {
                    "id": "openai-codex/gpt-5.6-terra",
                    "name": "GPT-5.6 Terra",
                    "provider": "openai-codex",
                },
                {
                    "id": "openai-codex/gpt-5.4",
                    "name": "GPT-5.4",
                    "provider": "openai-codex",
                },
                {
                    "id": "openai-codex/gpt-5.4-mini",
                    "name": "GPT-5.4 Mini",
                    "provider": "openai-codex",
                },
            ],
        )

    def test_llm_models_returns_codex_fallback_when_auth_configured_without_registry(self):
        try:
            from fastapi.testclient import TestClient
            from channels import web as web_module
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model="openai-codex/gpt-5.4", base_url=None),
        )
        channel = WebChannel(config=config, bus=MessageBus())

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_registry_path = Path(tmpdir) / "missing-models.generated.js"
            original_path = web_module._PIAI_MODELS_JS_PATH
            original_cache = dict(web_module._PIAI_PROVIDER_MODEL_CACHE)
            try:
                web_module._PIAI_MODELS_JS_PATH = missing_registry_path
                web_module._PIAI_PROVIDER_MODEL_CACHE.clear()
                with patch(
                    "channels.web.get_codex_oauth_status",
                    return_value={"configured": True, "provider": "openai-codex"},
                ):
                    response = TestClient(channel.app).get("/api/llm/models")
            finally:
                web_module._PIAI_MODELS_JS_PATH = original_path
                web_module._PIAI_PROVIDER_MODEL_CACHE.clear()
                web_module._PIAI_PROVIDER_MODEL_CACHE.update(original_cache)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        codex_models = [
            model
            for model in payload["models"]
            if model.get("provider") == "openai-codex"
        ]
        self.assertGreaterEqual(len(codex_models), 1)
        self.assertIn(
            "openai-codex/gpt-5.4",
            {model["id"] for model in codex_models},
        )
        self.assertTrue(
            {
                "openai-codex/gpt-5.6-sol",
                "openai-codex/gpt-5.6-luna",
                "openai-codex/gpt-5.6-terra",
            }.issubset({model["id"] for model in codex_models})
        )
        self.assertNotIn(
            "openai-codex/gpt-5.3-codex",
            {model["id"] for model in codex_models},
        )

    def test_llm_models_fetches_moonshot_provider_models_when_key_is_available(self):
        try:
            from fastapi.testclient import TestClient
            from channels.web import WebChannel
            from core.bus import MessageBus
        except Exception:
            raise unittest.SkipTest("Missing web channel dependencies.")

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model="moonshot/kimi-k2-thinking", base_url=None),
        )
        channel = WebChannel(config=config, bus=MessageBus())

        with patch.dict(
            "os.environ",
            {"MOONSHOT_API_KEY": "moonshot-secret"},
            clear=False,
        ), patch(
            "core.llm_utils.fetch_openai_compatible_models",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "moonshot/kimi-latest",
                        "name": "Kimi Latest",
                        "provider": "moonshot",
                    }
                ]
            ),
        ), patch(
            "channels.web.get_codex_oauth_status",
            return_value={"configured": False, "provider": "openai-codex"},
        ):
            response = TestClient(channel.app).get("/api/llm/models")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        moonshot_models = [
            model for model in payload["models"] if model.get("provider") == "moonshot"
        ]
        self.assertIn(
            "moonshot/kimi-k2-thinking",
            {model["id"] for model in moonshot_models},
        )
        self.assertIn(
            "moonshot/kimi-latest",
            {model["id"] for model in moonshot_models},
        )


if __name__ == "__main__":
    unittest.main()
