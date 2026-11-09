"""Offline acceptance for the image's real speech models; synthetic inputs only."""

import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

os.environ["WHISPER_BRIDGE_TOKEN"] = "synthetic-" + uuid.uuid4().hex
from fastapi.testclient import TestClient  # noqa: E402

import app as bridge  # noqa: E402


class RuntimeAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(bridge.app)
        cls.client = cls.context.__enter__()
        cls.headers = {"Authorization": "Bearer " + os.environ["WHISPER_BRIDGE_TOKEN"]}

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def test_health_has_offline_models_loaded(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        for key in ["loaded", "vosk_model", "tts"]:
            self.assertTrue(response.json()[key])

    def test_all_sensitive_routes_reject_anonymous_requests(self):
        self.assertEqual(self.client.get("/vosk-model").status_code, 401)
        self.assertEqual(self.client.post("/speak", json={"text": "synthetic"}).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/transcribe", files={"file": ("synthetic.wav", b"not audio", "audio/wav")}
            ).status_code,
            401,
        )

    def test_empty_audio_rejected(self):
        r = self.client.post(
            "/transcribe", headers=self.headers, files={"file": ("empty.wav", b"", "audio/wav")}
        )
        self.assertEqual(r.status_code, 400)

    def test_audio_size_limit(self):
        with patch.object(bridge, "MAX_BYTES", 4):
            r = self.client.post(
                "/transcribe",
                headers=self.headers,
                files={"file": ("large.wav", b"12345", "audio/wav")},
            )
            self.assertEqual(r.status_code, 413)

    def test_empty_speech_rejected(self):
        self.assertEqual(
            self.client.post("/speak", headers=self.headers, json={"text": "  \n  "}).status_code,
            400,
        )

    def test_speech_size_limit(self):
        with patch.object(bridge, "MAX_TTS_CHARS", 4):
            self.assertEqual(
                self.client.post(
                    "/speak", headers=self.headers, json={"text": "abcde"}
                ).status_code,
                413,
            )

    def test_invalid_audio_has_controlled_error(self):
        r = self.client.post(
            "/transcribe",
            headers=self.headers,
            files={"file": ("invalid.wav", b"not audio", "audio/wav")},
        )
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["detail"], "transcription failed")

    def test_real_whisper_accepts_synthetic_silence(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(b"\0\0" * 16000)
        r = self.client.post(
            "/transcribe",
            headers=self.headers,
            files={"file": ("silence.wav", buffer.getvalue(), "audio/wav")},
        )
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIsInstance(d["text"], str)
        self.assertIsInstance(d["duration_ms"], int)
        self.assertGreaterEqual(d["duration_ms"], 0)
        self.assertLessEqual(d["duration_ms"], 2000)

    def test_real_piper_synthesizes_synthetic_text(self):
        r = self.client.post(
            "/speak", headers=self.headers, json={"text": "This is a synthetic security check."}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("audio/wav", r.headers["content-type"])
        with wave.open(io.BytesIO(r.content), "rb") as f:
            self.assertEqual(f.getnchannels(), 1)
            self.assertGreater(f.getnframes(), 0)
            self.assertGreater(f.getframerate(), 0)

    def test_real_speech_round_trip(self):
        spoken = self.client.post(
            "/speak",
            headers=self.headers,
            json={"text": "This is a security test of the local speech service."},
        )
        self.assertEqual(spoken.status_code, 200)
        recognized = self.client.post(
            "/transcribe",
            headers=self.headers,
            files={"file": ("synthetic.wav", spoken.content, "audio/wav")},
        )
        self.assertEqual(recognized.status_code, 200)
        self.assertIn("security", recognized.json()["text"].lower())

    def test_authorized_vosk_model_matches_baked_asset(self):
        r = self.client.get("/vosk-model", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            hashlib.sha256(r.content).hexdigest(),
            hashlib.sha256(Path(bridge.VOSK_MODEL_PATH).read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeAcceptance)
    )
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    summary = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "pip_check_exit": check.returncode,
        "pip_check_output": check.stdout.strip(),
        "inputs": "synthetic only",
        "real_models_exercised": ["faster-whisper small", "Piper voice", "Vosk static archive"],
        "network": "none",
    }
    print(json.dumps(summary, sort_keys=True))
    if os.environ.get("WHISPER_TEST_SUMMARY"):
        Path(os.environ["WHISPER_TEST_SUMMARY"]).write_text(json.dumps(summary, indent=2) + "\n")
    raise SystemExit(
        0 if result.wasSuccessful() and not result.skipped and check.returncode == 0 else 1
    )
