#!/usr/bin/env python3
"""Automated integration test for the self-correction chatbot."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import base64
import io
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import api
import whatsapp_agent
from PIL import Image
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

    def test_media_part_validates_image_bytes_and_local_path(self) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color="red").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()

        encoded = api.media_part(image_bytes, "image/png")["inline_data"]["data"]
        self.assertEqual(base64.b64decode(encoded), image_bytes)

        image_path = Path(self.temp_dir.name) / "image.png"
        image_path.write_bytes(image_bytes)
        path_payload = api.media_part(image_path, "image/png")["inline_data"]["data"]
        self.assertEqual(base64.b64decode(path_payload), image_bytes)

        with self.assertRaises(api.BridgeError):
            api.media_part(b"not an image", "image/png")

    def test_media_part_encodes_pil_images(self) -> None:
        image = Image.new("RGB", (2, 2), color="purple")

        media = api.media_part(image, "image/png")

        self.assertEqual(media["inline_data"]["mime_type"], "image/png")
        with Image.open(io.BytesIO(base64.b64decode(media["inline_data"]["data"]))) as decoded:
            self.assertEqual(decoded.size, (2, 2))
            self.assertEqual(decoded.getpixel((0, 0)), (128, 0, 128))

    def test_media_part_converts_alpha_pil_images_for_jpeg(self) -> None:
        image = Image.new("RGBA", (2, 2), color=(255, 0, 0, 128))

        media = api.media_part(image, "image/jpeg")

        with Image.open(io.BytesIO(base64.b64decode(media["inline_data"]["data"]))) as decoded:
            self.assertEqual(decoded.mode, "RGB")
            self.assertEqual(decoded.size, (2, 2))

    def test_model_picker_uses_active_model_for_retired_overrides(self) -> None:
        for retired_model in ("gemini-2.0-flash", "gemini-3.8-flash", "not-a-gemini-model"):
            with self.subTest(model=retired_model), patch.dict(os.environ, {"GEMINI_MODEL": retired_model}):
                self.assertEqual(api._pick_model("gemini-3.1-pro-preview"), api.DEFAULT_MODEL)

    def test_gemini_models_and_fallbacks_use_active_allowlist(self) -> None:
        active_models = {
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash-lite",
        }

        self.assertEqual(api.DEFAULT_MODEL, "gemini-3.5-flash-lite")
        self.assertEqual(api.SUPPORTED_MODELS, active_models)
        self.assertEqual(
            api.FALLBACK_MODELS,
            ["gemini-3.5-flash-lite", "gemini-3.1-pro-preview"],
        )

    def test_whatsapp_state_round_trips_atomically(self) -> None:
        state_path = Path(self.temp_dir.name) / "wa_state.json"
        with patch.object(whatsapp_agent, "STATE_FILE", state_path):
            expected = {"offset": 12, "since": 34, "handled": ["wamid.1"]}
            whatsapp_agent.save_state(expected.copy())
            self.assertEqual(whatsapp_agent.load_state(), expected)

    def test_whatsapp_defaults_to_ayan_without_user_registry(self) -> None:
        with patch.object(whatsapp_agent, "BASE_DIR", Path(self.temp_dir.name)), \
             patch.dict(os.environ, {"CHATBOT_USER": ""}):
            self.assertEqual(whatsapp_agent.find_user(), "Ayan")

    def test_whatsapp_download_streams_and_caps_media(self) -> None:
        class MetadataResponse:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, str]:
                return {"url": "https://media.example.test/item", "mime_type": "audio/ogg; codecs=opus"}

        class MediaResponse:
            headers = {"Content-Length": "3"}

            def __init__(self, chunks: list[bytes]):
                self.chunks = chunks
                self.closed = False

            def raise_for_status(self) -> None:
                pass

            def iter_content(self, chunk_size: int):
                return iter(self.chunks)

            def close(self) -> None:
                self.closed = True

        metadata = MetadataResponse()
        media = MediaResponse([b"ab", b"c"])
        with patch.object(whatsapp_agent, "api_call", return_value=metadata), \
             patch.object(whatsapp_agent.HTTP, "get", return_value=media) as get_media:
            payload, mime = whatsapp_agent.download("media-id")
        self.assertEqual((payload, mime), (b"abc", "audio/ogg"))
        get_media.assert_called_once_with(
            "https://media.example.test/item",
            headers=whatsapp_agent.HDR,
            timeout=30,
            stream=True,
        )
        self.assertTrue(media.closed)

        oversized = MediaResponse([b"unused"])
        oversized.headers = {"Content-Length": str(whatsapp_agent.MAX_MEDIA_BYTES + 1)}
        with patch.object(whatsapp_agent, "api_call", return_value=metadata), \
             patch.object(whatsapp_agent.HTTP, "get", return_value=oversized):
            with self.assertRaises(ValueError):
                whatsapp_agent.download("media-id")
        self.assertTrue(oversized.closed)

    def test_whatsapp_text_gets_double_newline_sentence_format(self) -> None:
        formatted = whatsapp_agent.clean_whatsapp_text("First sentence. Second sentence!\n1. One\n2. Two")

        self.assertEqual(
            formatted,
            "First sentence.\n\nSecond sentence!\n\n1. One\n\n2. Two",
        )

    def test_whatsapp_image_digest_sends_prompt_and_base64_image(self) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color="blue").save(image_buffer, format="JPEG")
        image_bytes = image_buffer.getvalue()

        with patch.dict(os.environ, {"GEMINI_MODEL": ""}):
            self.assertEqual(api._pick_model(whatsapp_agent.cfg_model()), api.DEFAULT_MODEL)

        with patch.object(api, "_call_gemini", return_value=("A blue image.", {})) as call:
            result = whatsapp_agent.digest(
                "image", image_bytes, "IMAGE/JPEG; charset=binary", text="What color is it?"
            )

        self.assertEqual(result, "A blue image.")
        contents = call.call_args.args[2]
        parts = contents[0]["parts"]
        self.assertEqual(parts[0]["text"], "What color is it?")
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/jpeg")
        with Image.open(io.BytesIO(base64.b64decode(parts[1]["inline_data"]["data"]))) as decoded:
            self.assertEqual(decoded.size, (2, 2))

    def test_gemini_generate_uses_v1beta_model_and_multipart_json(self) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color="green").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()
        media = api.media_part(image_bytes, "image/png")
        audio = api.media_part(b"sample voice note", "audio/ogg")

        class SuccessfulResponse:
            status_code = 200
            ok = True

        with patch.dict(os.environ, {"GEMINI_THINKING": "default"}), \
             patch.object(api, "_load_think_cache", return_value={}), \
             patch.object(api, "_save_think_cache"), \
             patch.object(api.requests, "post", return_value=SuccessfulResponse()) as post:
            api._generate(
                "gemini-3.5-flash-lite",
                "test-key",
                "",
                [{"role": "user", "parts": [{"text": "Describe this image."}, media, audio]}],
                0.1,
                10,
            )

        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            url,
            f"{api.GEMINI_URL}/models/gemini-3.5-flash-lite:generateContent",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "Describe this image."})
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), image_bytes)
        self.assertEqual(parts[2]["inline_data"]["mime_type"], "audio/ogg")
        self.assertEqual(base64.b64decode(parts[2]["inline_data"]["data"]), b"sample voice note")

    def test_ipc_reads_back_to_back_stdout_turns_without_stale_bytes(self) -> None:
        proc = whatsapp_agent.ChatbotProc(Path("unused"), "Ayan")
        proc.q.put(b"first turn\n<<READY>>\nsecond turn\n<<READY>>\n")

        self.assertEqual(proc._read_ready(1), "first turn\n")
        self.assertEqual(proc._read_ready(1), "second turn\n")
        self.assertEqual(proc.stdout_buffer, bytearray())

    def test_gemini_404_reports_actual_api_error_detail(self) -> None:
        error_body = json.dumps({"error": {"message": "Requested model is not available."}})

        self.assertEqual(
            api._friendly_status(404, error_body),
            "Gemini returned HTTP 404: Requested model is not available.",
        )

    def test_ipc_error_response_replaces_stale_response_atomically(self) -> None:
        response_path = Path(self.temp_dir.name) / "response.json"
        response_path.write_text(json.dumps({"error": "old cached HTTP 404"}), encoding="utf-8")

        self.assertTrue(api.write_error(str(response_path), "Current bridge failure."))
        self.assertEqual(
            json.loads(response_path.read_text(encoding="utf-8")),
            {"error": "Current bridge failure."},
        )

    def test_main_returns_error_json_with_success_exit_status(self) -> None:
        request_path = Path(self.temp_dir.name) / "request.json"
        response_path = Path(self.temp_dir.name) / "response.json"
        request_path.write_text(json.dumps({
            "model": "gemini-test",
            "messages": [{"role": "user", "content": "describe this image"}],
        }), encoding="utf-8")

        with patch.object(api, "run_turn", side_effect=api.BridgeError("Invalid image payload.")):
            self.assertEqual(api.main(str(request_path), str(response_path)), 0)

        self.assertEqual(json.loads(response_path.read_text(encoding="utf-8")),
                         {"error": "Invalid image payload."})


if __name__ == "__main__":
    unittest.main(verbosity=2)
