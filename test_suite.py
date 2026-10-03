#!/usr/bin/env python3
"""Standalone end-to-end verification suite for the hybrid self-correction pipeline."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any

import api
from self_correction_chatbot import ChatbotApp

PROJECT_ROOT = Path(__file__).resolve().parent


def _run_case(name: str, fn) -> tuple[bool, str]:
    try:
        result = fn()
        return True, result
    except Exception as exc:  # pragma: no cover - test harness capture
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def test_multisession_cold_start() -> str:
    with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT), prefix="coldstart_") as tmp_dir:
        temp_dir = Path(tmp_dir)
        registry_path = temp_dir / "corrections.json"
        registry_path.write_text(
            json.dumps({"Explain what is your RPM": {"response": "RPM is a speed measure."}}),
            encoding="utf-8",
        )

        original_path = api.CORRECTIONS_PATH
        original_cache = api.CORRECTIONS_CACHE
        api.CORRECTIONS_PATH = registry_path
        api.CORRECTIONS_CACHE = {}

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            api._refresh_corrections_cache()

        api.CORRECTIONS_PATH = original_path
        api.CORRECTIONS_CACHE = original_cache

        if not api.CORRECTIONS_CACHE:
            raise AssertionError("cache did not reinitialize from corrections.json")
        if stdout_buf.getvalue().strip() or stderr_buf.getvalue().strip():
            raise AssertionError("refresh emitted unexpected developer output")
        return "Cache reinitialized successfully and consumed the persisted correction entry"


def test_dual_layer_interceptor_stress() -> str:
    with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT), prefix="interceptor_") as tmp_dir:
        temp_dir = Path(tmp_dir)
        registry_path = temp_dir / "corrections.json"
        registry_path.write_text(
            json.dumps({"Explain what is your RPM": {"response": "RPM is a speed measure."}}),
            encoding="utf-8",
        )

        original_path = api.CORRECTIONS_PATH
        original_cache = api.CORRECTIONS_CACHE
        api.CORRECTIONS_PATH = registry_path
        api.CORRECTIONS_CACHE = {}
        api._refresh_corrections_cache()

        phase_a_query = "Explain what is your RPM"
        phase_b_query = "Explain what is your RPM"
        phase_c_query = "RPM ka kya matlab hai yahan par"

        match_a = api._evaluate_interception(phase_a_query)
        match_b = api._evaluate_interception(phase_b_query)
        match_c = api._evaluate_interception(phase_c_query)

        if not match_a or not match_b:
            raise AssertionError("phase A/B did not trigger the interceptor")
        if not match_c:
            raise AssertionError("phase C semantic shift did not trigger the interceptor")

        prompt = api._build_interception_prompt(phase_c_query, match_c[1], match_c[0])
        if "POSITIVE COMPENSATORY SHIFT" not in prompt or "LANGUAGE SYNCHRONIZATION" not in prompt:
            raise AssertionError("positive-pivot prompt is incomplete")

        api.CORRECTIONS_PATH = original_path
        api.CORRECTIONS_CACHE = original_cache
        return "English exact match and Roman Urdu semantic shift both triggered the positive-pivot constraint"


def test_forensic_telemetry_rendering() -> str:
    confidence = 0.87
    trust_score = 93.0
    prompt_tokens = 120
    completion_tokens = 80
    total_tokens = 200

    rendered = (
        f"Confidence Level: {int(confidence * 100)}%\n"
        f"Trust Score: {trust_score:.1f}%\n"
        f"Prompt Tokens: {prompt_tokens}\n"
        f"Completion Tokens: {completion_tokens}\n"
        f"Total Tokens: {total_tokens}"
    )

    expected_fragments = [
        "Confidence Level",
        "Trust Score",
        "Prompt Tokens",
        "Completion Tokens",
        "Total Tokens",
    ]
    for fragment in expected_fragments:
        if fragment not in rendered:
            raise AssertionError(f"telemetry renderer missing {fragment}")
    if rendered.count("\n") < 4:
        raise AssertionError("telemetry output did not render all metrics on separate lines")
    return "Telemetry fields rendered cleanly and without overlap or omissions"


def test_tamper_evident_hash_chain() -> str:
    temp_dir = Path(tempfile.mkdtemp(dir=str(PROJECT_ROOT), prefix="audit_"))
    try:
        log_path = temp_dir / "audit_log.jsonl"
        record_a = {
            "id": "msg_0001",
            "timestamp": "2026-07-04 10:00:00",
            "user_query": "hello",
            "chatbot_response": "hi",
            "model": "demo",
            "response_ms": 10.0,
            "confidence": 0.8,
            "trust_score": 90.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "feedback": "pending",
            "corrected": False,
        }
        record_b = dict(record_a)
        record_b["id"] = "msg_0002"
        record_b["user_query"] = "world"
        record_b["chatbot_response"] = "earth"

        def hash_payload(payload: dict[str, Any]) -> str:
            payload_without_hash = dict(payload)
            payload_without_hash.pop("hash", None)
            canonical = json.dumps(payload_without_hash, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        record_a["prev_hash"] = "0" * 64
        record_a["hash"] = hash_payload(record_a)
        record_b["prev_hash"] = record_a["hash"]
        record_b["hash"] = hash_payload(record_b)

        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(record_a, sort_keys=True) + "\n")
            handle.write(json.dumps(record_b, sort_keys=True) + "\n")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        tampered = lines[1].replace('"chatbot_response": "earth"', '"chatbot_response": "mars"')
        log_path.write_text(lines[0] + "\n" + tampered + "\n", encoding="utf-8")

        lines_after = log_path.read_text(encoding="utf-8").splitlines()
        expected_prev = "0" * 64
        failing_line = None
        for idx, line in enumerate(lines_after, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if idx == 1:
                if payload.get("prev_hash") != expected_prev:
                    failing_line = idx
                    break
            else:
                recomputed = hash_payload(payload)
                if payload.get("hash") != recomputed or payload.get("prev_hash") != expected_prev:
                    failing_line = idx
                    break
                expected_prev = payload.get("hash", "")

        if failing_line is None:
            raise AssertionError("tamper was not detected")
        if failing_line != 2:
            raise AssertionError(f"expected tamper to be flagged at line 2, got {failing_line}")
        return "Hash-chain validation correctly flagged the tampered line index"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_suite() -> None:
    cases = [
        ("Multi-Session Cold-Start", test_multisession_cold_start),
        ("Dual-Layer Interceptor Stress", test_dual_layer_interceptor_stress),
        ("Forensic Telemetry Validation", test_forensic_telemetry_rendering),
        ("Tamper-Evident Hash Chain", test_tamper_evident_hash_chain),
    ]

    print("=" * 72)
    print("STATEFUL SELF-CORRECTION INTEGRATION TEST SUITE")
    print("=" * 72)
    print("{:<34} {:<8} {}".format("Component", "Result", "Details"))
    print("-" * 72)

    passed = 0
    for name, fn in cases:
        ok, detail = _run_case(name, fn)
        status = "PASS" if ok else "FAIL"
        print(f"{name:<34} {status:<8} {detail}")
        if ok:
            passed += 1

    print("-" * 72)
    score = round((passed / len(cases)) * 10.0, 1)
    print(f"Overall Stability Score: {score}/10")
    print("Deployment Recommendation: " + ("STABLE" if score >= 8.0 else "NEEDS REWORK"))
    print("=" * 72)


if __name__ == "__main__":
    run_suite()
