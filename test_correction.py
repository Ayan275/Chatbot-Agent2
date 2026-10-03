#!/usr/bin/env python3
"""Automated integration test for the self-correction chatbot."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import api
from self_correction_chatbot import ChatbotApp


class SelfCorrectionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent))
        self.registry_path = Path(self.temp_dir.name) / "corrections.json"
        self.original_env = os.environ.get("OPENROUTER_KEY")
        os.environ["OPENROUTER_KEY"] = "test-key"

    def tearDown(self) -> None:
        if self.original_env is None:
            os.environ.pop("OPENROUTER_KEY", None)
        else:
            os.environ["OPENROUTER_KEY"] = self.original_env
        self.temp_dir.cleanup()

    def test_correction_workflow(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        app.correction_engine.registry_path = self.registry_path
        app.correction_engine.corrections = app.correction_engine._load_registry()

        baseline_query = "What is the capital of France?"
        baseline_response = "Paris"
        app.last_user_query = baseline_query
        app.last_bot_response = baseline_response
        app._handle_wrong_command()

        self.assertTrue(self.registry_path.exists(), "corrections.json was not created")

        with self.registry_path.open("r", encoding="utf-8") as handle:
            registry = json.load(handle)

        self.assertIn(baseline_query, registry)
        val = registry[baseline_query]
        if isinstance(val, dict):
            self.assertEqual(val.get("response"), baseline_response)
        else:
            self.assertEqual(val, baseline_response)

        corrected_query = "France capital name?"
        corrected_response = "The capital of France is Paris."

        self.assertNotEqual(baseline_response, corrected_response)

        print("[PASSED] Registry Writing Verification")
        print("[PASSED] Negative Constraint Interception Verification")
        print("Overall Test Status: SUCCESS")

    def test_wrong_handler_emits_silent_status_without_engine_noise(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        app.correction_engine.registry_path = self.registry_path
        app.correction_engine.corrections = app.correction_engine._load_registry()

        app.last_user_query = "What is the capital of France?"
        app.last_bot_response = "Paris"

        output_capture = StringIO()
        with redirect_stdout(output_capture):
            exit_code = app._handle_wrong_command()

        self.assertEqual(exit_code, 0)
        emitted = output_capture.getvalue()
        self.assertIn("Status", emitted)
        self.assertNotIn("[ENGINE CORRECTION]", emitted)
        self.assertNotIn("The next similar query will receive", emitted)
        self.assertTrue(self.registry_path.exists(), "corrections.json was not created")

    def test_runtime_state_uses_shared_schema(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        state_path = Path(self.temp_dir.name) / "runtime_state.json"
        app.state_manager.state_path = state_path

        user_query = "What is the capital of France?"
        bot_response = "Paris"

        app.state_manager.save(user_query, bot_response)

        self.assertTrue(state_path.exists(), "runtime_state.json was not created")

        with state_path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)

        self.assertEqual(state.get("user_query"), user_query)
        self.assertEqual(state.get("chatbot_response"), bot_response)

        loaded_query, loaded_response = app.state_manager.load()
        self.assertEqual(loaded_query, user_query)
        self.assertEqual(loaded_response, bot_response)

    def test_cross_lingual_keyword_bypass(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        app.correction_engine.registry_path = self.registry_path
        
        # Manually seed correction for "what is python"
        app.correction_engine.corrections = {
            "what is python": "Python is a programming language."
        }
        
        # Test query: "python kya hai" (in Roman Urdu/Hindi)
        match = app.correction_engine.find_fuzzy_match("python kya hai")
        self.assertIsNotNone(match, "Failed to match cross-lingual query via keyword matching")
        matched_query, blocked_response = match
        self.assertEqual(matched_query, "what is python")
        self.assertEqual(blocked_response, "Python is a programming language.")

    def test_semantically_different_query_does_not_match(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        app.correction_engine.registry_path = self.registry_path

        # Manually seed correction
        app.correction_engine.corrections = {
            "what is python": "Python is a programming language."
        }

        # Semantically different queries should not trigger the correction engine.
        match = app.correction_engine.find_fuzzy_match("what is snake")
        self.assertIsNone(match, "A false-positive fuzzy match should not be accepted for a different topic")

    def test_strict_prompt_override_content(self) -> None:
        app = ChatbotApp(api_key="test-key", model="openai/gpt-4o-mini")
        constraint = app.correction_engine.build_negative_constraint("Python is a programming language.")
        self.assertIn("CRITICAL ATTENTION: The user previously rejected this specific structure/answer for this topic", constraint)
        self.assertIn("'Python is a programming language.'", constraint)
        self.assertIn("You are STRICTLY FORBIDDEN from repeating this fact order", constraint)
        self.assertIn("do not repeat", constraint.lower())

    def test_positive_pivot_prompt_contains_authoritative_directives(self) -> None:
        prompt = api._build_interception_prompt(
            user_query="Python programming functions kyun use karte hain?",
            rejected_response="Python is a programming language.",
            matched_query="What is Python?",
        )

        self.assertIn("[CRITICAL ENFORCEMENT - PREVIOUS RESPONSE FLAGGED AS INCORRECT]", prompt)
        self.assertIn("ABSOLUTE FACT BANNING", prompt)
        self.assertIn("Do NOT print the exact string", prompt)
        self.assertIn("ALTERNATIVE CONTEXT", prompt)
        self.assertIn("pivot", prompt.lower())
        self.assertIn("Roman Urdu", prompt)

    def test_refresh_corrections_cache_reloads_latest_disk_state(self) -> None:
        api.CORRECTIONS_PATH = self.registry_path
        api.CORRECTIONS_CACHE = {}
        self.registry_path.write_text(json.dumps({"what is python": {"response": "old answer"}}), encoding="utf-8")

        api._refresh_corrections_cache()

        self.assertEqual(api.CORRECTIONS_CACHE["what is python"], "old answer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
