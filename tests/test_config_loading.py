import json
import unittest
from pathlib import Path
from unittest.mock import patch


class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        self.config_path = Path("limebot.json")
        self.original_exists = self.config_path.exists()
        self.original_content = (
            self.config_path.read_text(encoding="utf-8")
            if self.original_exists
            else None
        )

    def tearDown(self):
        if self.original_exists:
            self.config_path.write_text(self.original_content or "", encoding="utf-8")
        elif self.config_path.exists():
            self.config_path.unlink()

        import config as config_module

        config_module._cached_config = None

    def _load_config_with_env(self, overrides):
        import config as config_module

        config_module._cached_config = None
        with patch.dict("os.environ", overrides, clear=False):
            return config_module.load_config(force_reload=True)

    def test_empty_json_model_does_not_override_env_model(self):
        self.config_path.write_text(
            json.dumps({"llm": {"model": ""}}, indent=2),
            encoding="utf-8",
        )

        import config as config_module

        config_module._cached_config = None
        with patch.dict("os.environ", {"LLM_MODEL": "nvidia/llama/4-scout"}, clear=False):
            loaded = config_module.load_config(force_reload=True)

        self.assertEqual(loaded.llm.model, "nvidia/llama/4-scout")

    def test_json_model_is_ignored_when_env_model_exists(self):
        self.config_path.write_text(
            json.dumps({"llm": {"model": "gemini/gemini-1.5-flash"}}, indent=2),
            encoding="utf-8",
        )

        import config as config_module

        config_module._cached_config = None
        with patch.dict("os.environ", {"LLM_MODEL": "nvidia/moonshotai/kimi-k2-thinking"}, clear=False):
            loaded = config_module.load_config(force_reload=True)

        self.assertEqual(loaded.llm.model, "nvidia/moonshotai/kimi-k2-thinking")

    def test_json_model_is_ignored_when_env_model_missing(self):
        self.config_path.write_text(
            json.dumps({"llm": {"model": "nvidia/moonshotai/kimi-k2-thinking"}}, indent=2),
            encoding="utf-8",
        )

        import config as config_module

        config_module._cached_config = None
        with patch.dict("os.environ", {"LLM_MODEL": ""}, clear=False):
            loaded = config_module.load_config(force_reload=True)

        self.assertEqual(loaded.llm.model, "gemini/gemini-3.6-flash")

    def test_fallback_models_are_loaded_from_env_and_deduped_against_primary(self):
        import config as config_module

        config_module._cached_config = None
        with patch.dict(
            "os.environ",
            {
                "LLM_MODEL": "openai/gpt-4o-mini",
                "LLM_FALLBACK_MODELS": "openai/gpt-4o-mini,anthropic/claude-3-5-haiku-latest, gemini/gemini-2.0-flash",
            },
            clear=False,
        ):
            loaded = config_module.load_config(force_reload=True)

        self.assertEqual(loaded.llm.model, "openai/gpt-4o-mini")
        self.assertEqual(
            loaded.llm.fallback_models,
            ["anthropic/claude-3-5-haiku-latest", "gemini/gemini-2.0-flash"],
        )

    def test_telegram_config_is_loaded_from_env(self):
        loaded = self._load_config_with_env(
            {
                "ENABLE_TELEGRAM": "true",
                "TELEGRAM_BOT_TOKEN": "telegram-token",
                "TELEGRAM_ALLOW_FROM": "123,456",
                "TELEGRAM_ALLOW_CHATS": "-1001,-1002",
                "TELEGRAM_POLL_TIMEOUT": "45",
            }
        )

        self.assertTrue(loaded.telegram.enabled)
        self.assertEqual(loaded.telegram.token, "telegram-token")
        self.assertEqual(loaded.telegram.allow_from, ["123", "456"])
        self.assertEqual(loaded.telegram.allow_chats, ["-1001", "-1002"])
        self.assertEqual(loaded.telegram.poll_timeout, 45)

    def test_ai_harness_defaults_to_fast_mode(self):
        loaded = self._load_config_with_env(
            {
                "LIMEBOT_AI_HARNESS_MODE": "",
                "LIMEBOT_FAST_RAG_TIMEOUT": "",
                "LIMEBOT_BALANCED_RAG_TIMEOUT": "",
                "LIMEBOT_FAST_DISABLE_TOOLS_FOR_CASUAL": "",
                "LIMEBOT_ENABLE_TOOL_SHORTLIST": "",
            }
        )

        self.assertEqual(loaded.ai_harness.mode, "fast")
        self.assertAlmostEqual(loaded.ai_harness.fast_rag_timeout_s, 0.08)
        self.assertAlmostEqual(loaded.ai_harness.balanced_rag_timeout_s, 0.2)
        self.assertAlmostEqual(loaded.ai_harness.rag_timeout_s, 0.08)
        self.assertTrue(loaded.ai_harness.fast_disable_tools_for_casual)
        self.assertFalse(loaded.tool_shortlist_enabled)

    def test_tool_recovery_attempts_are_bounded_and_configurable(self):
        loaded = self._load_config_with_env(
            {"TOOL_RECOVERY_MAX_ATTEMPTS": "5"}
        )
        self.assertEqual(loaded.tool_recovery_max_attempts, 5)

        loaded = self._load_config_with_env(
            {"TOOL_RECOVERY_MAX_ATTEMPTS": "0"}
        )
        self.assertEqual(loaded.tool_recovery_max_attempts, 3)

    def test_video_whisper_is_explicitly_opt_in(self):
        disabled = self._load_config_with_env({"VIDEO_WHISPER_ENABLED": "false"})
        self.assertFalse(disabled.video.whisper_enabled)
        enabled = self._load_config_with_env({"VIDEO_WHISPER_ENABLED": "true"})
        self.assertTrue(enabled.video.whisper_enabled)

    def test_image_generation_defaults_to_gpt_image_2(self):
        loaded = self._load_config_with_env({"IMAGE_GENERATION_MODEL": ""})

        self.assertEqual(loaded.image_generation.model, "openai/gpt-image-2")

    def test_ai_harness_fast_mode_uses_fast_timeout(self):
        loaded = self._load_config_with_env(
            {
                "LIMEBOT_AI_HARNESS_MODE": "fast",
                "LIMEBOT_FAST_RAG_TIMEOUT": "0.05",
                "LIMEBOT_BALANCED_RAG_TIMEOUT": "0.25",
                "LIMEBOT_FAST_DISABLE_TOOLS_FOR_CASUAL": "false",
            }
        )

        self.assertEqual(loaded.ai_harness.mode, "fast")
        self.assertAlmostEqual(loaded.ai_harness.fast_rag_timeout_s, 0.05)
        self.assertAlmostEqual(loaded.ai_harness.balanced_rag_timeout_s, 0.25)
        self.assertAlmostEqual(loaded.ai_harness.rag_timeout_s, 0.05)
        self.assertFalse(loaded.ai_harness.fast_disable_tools_for_casual)

    def test_ai_harness_invalid_mode_and_timeout_fall_back_to_defaults(self):
        loaded = self._load_config_with_env(
            {
                "LIMEBOT_AI_HARNESS_MODE": "warp-speed",
                "LIMEBOT_FAST_RAG_TIMEOUT": "nope",
                "LIMEBOT_BALANCED_RAG_TIMEOUT": "still-nope",
                "LIMEBOT_FAST_DISABLE_TOOLS_FOR_CASUAL": "maybe",
                "LIMEBOT_ENABLE_TOOL_SHORTLIST": "maybe",
            }
        )

        self.assertEqual(loaded.ai_harness.mode, "fast")
        self.assertAlmostEqual(loaded.ai_harness.fast_rag_timeout_s, 0.08)
        self.assertAlmostEqual(loaded.ai_harness.balanced_rag_timeout_s, 0.2)
        self.assertAlmostEqual(loaded.ai_harness.rag_timeout_s, 0.08)
        self.assertTrue(loaded.ai_harness.fast_disable_tools_for_casual)
        self.assertFalse(loaded.tool_shortlist_enabled)

    def test_tool_shortlist_is_explicit_opt_in_in_fast_mode(self):
        loaded = self._load_config_with_env(
            {
                "LIMEBOT_AI_HARNESS_MODE": "fast",
                "LIMEBOT_ENABLE_TOOL_SHORTLIST": "true",
            }
        )

        self.assertEqual(loaded.ai_harness.mode, "fast")
        self.assertTrue(loaded.tool_shortlist_enabled)

    def test_ai_harness_explicit_balanced_mode_preserves_full_schema(self):
        loaded = self._load_config_with_env(
            {
                "LIMEBOT_AI_HARNESS_MODE": "balanced",
                "LIMEBOT_ENABLE_TOOL_SHORTLIST": "",
            }
        )

        self.assertEqual(loaded.ai_harness.mode, "balanced")
        self.assertAlmostEqual(loaded.ai_harness.rag_timeout_s, 0.2)
        self.assertFalse(loaded.tool_shortlist_enabled)

    def test_approval_policy_defaults_to_manual(self):
        loaded = self._load_config_with_env(
            {"APPROVAL_POLICY_PROFILE": "", "AUTONOMOUS_MODE": "false"}
        )

        self.assertEqual(loaded.approval_policy_profile, "manual")
        self.assertFalse(loaded.autonomous_mode)

    def test_legacy_autonomous_mode_maps_to_named_policy(self):
        loaded = self._load_config_with_env(
            {"APPROVAL_POLICY_PROFILE": "", "AUTONOMOUS_MODE": "true"}
        )

        self.assertEqual(loaded.approval_policy_profile, "autonomous")
        self.assertTrue(loaded.autonomous_mode)

    def test_named_approval_policy_takes_precedence_over_legacy_toggle(self):
        loaded = self._load_config_with_env(
            {"APPROVAL_POLICY_PROFILE": "review", "AUTONOMOUS_MODE": "true"}
        )

        self.assertEqual(loaded.approval_policy_profile, "review")
        self.assertFalse(loaded.autonomous_mode)

    def test_invalid_approval_policy_falls_back_to_manual(self):
        loaded = self._load_config_with_env(
            {"APPROVAL_POLICY_PROFILE": "unlimited", "AUTONOMOUS_MODE": "true"}
        )

        self.assertEqual(loaded.approval_policy_profile, "manual")
        self.assertFalse(loaded.autonomous_mode)
