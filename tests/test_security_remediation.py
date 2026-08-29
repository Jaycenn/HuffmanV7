#!/usr/bin/env python3
"""Focused regression tests for the post-evaluation remediation branch."""

import contextlib
import ctypes
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class RemediationTests(unittest.TestCase):
    def test_corpus_download_uses_default_tls_verification(self):
        path = os.path.join(ROOT, "tools", "corpus_manifest.py")
        spec = importlib.util.spec_from_file_location(
            "remediation_corpus_manifest", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sentinel = object()

        with mock.patch.object(
                module.urllib.request, "urlopen",
                return_value=sentinel) as opened:
            result = module._urlopen(
                "https://example.invalid/corpus", headers={"Range": "bytes=0-3"})

        self.assertIs(result, sentinel)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/corpus")
        self.assertEqual(opened.call_args.kwargs, {"timeout": 120})

        with mock.patch.object(module.urllib.request, "urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                module._urlopen("http://example.invalid/corpus")
            opened.assert_not_called()

    def test_container_fallback_is_logged_and_remains_lossless(self):
        import afc2
        import containers

        data = b"post-evaluation fallback test " * 20
        with mock.patch.object(afc2, "_looks_like_container", return_value=True):
            with mock.patch.object(
                    containers, "compress_container",
                    side_effect=RuntimeError("simulated analysis failure")):
                with self.assertLogs("afc2", level="WARNING") as captured:
                    blob = afc2.compress_bytes(
                        data, backend="python", container_aware=True)

        self.assertEqual(afc2.decompress_bytes(blob), data)
        self.assertIn("falling back", "\n".join(captured.output))

    def test_artifact_rollback_failure_is_logged(self):
        import config

        original = (
            config.DB_BACKEND,
            config.STORAGE_BACKEND,
            config.DATABASE_PATH,
            config.RESULT_STORAGE_DIR,
            config.SECRET_KEY_PATH,
        )
        with tempfile.TemporaryDirectory(prefix="bytesize-remediation-") as tmp:
            config.DB_BACKEND = "sqlite"
            config.STORAGE_BACKEND = "local"
            config.DATABASE_PATH = os.path.join(tmp, "test.sqlite3")
            config.RESULT_STORAGE_DIR = os.path.join(tmp, "results")
            config.SECRET_KEY_PATH = os.path.join(tmp, "secret.key")
            sys.modules.pop("app", None)
            import app

            @contextlib.contextmanager
            def reservation(*_args, **_kwargs):
                yield object()

            try:
                with mock.patch.object(app.db, "storage_reservation", reservation):
                    with mock.patch.object(
                            app.artifact_store, "write",
                            return_value=("a" * 32, "b" * 64)):
                        with mock.patch.object(
                                app.db, "add_stored_artifact",
                                side_effect=RuntimeError(
                                    "simulated database failure")):
                            with mock.patch.object(
                                    app.artifact_store, "delete",
                                    side_effect=OSError(
                                        "simulated cleanup failure")):
                                with mock.patch.object(
                                        app.db, "delete_history_group"):
                                    with self.assertLogs(
                                            "app", level="ERROR") as captured:
                                        with self.assertRaisesRegex(
                                                RuntimeError,
                                                "database failure"):
                                            app._persist_history_artifact(
                                                1, 2, "result.afc", b"result")
            finally:
                (config.DB_BACKEND,
                 config.STORAGE_BACKEND,
                 config.DATABASE_PATH,
                 config.RESULT_STORAGE_DIR,
                 config.SECRET_KEY_PATH) = original
                sys.modules.pop("app", None)

        self.assertIn("rolling back", "\n".join(captured.output))

    def test_supabase_multipart_rollback_failure_is_logged(self):
        import storage_supabase

        class FailingBucket:
            def __init__(self):
                self.uploads = 0

            def upload(self, _name, _blob, _options):
                self.uploads += 1
                if self.uploads == 2:
                    raise OSError("simulated upload failure")

            def remove(self, _names):
                raise OSError("simulated cleanup failure")

        bucket = FailingBucket()
        with mock.patch.object(storage_supabase, "_bucket", return_value=bucket):
            with mock.patch.object(storage_supabase, "_CHUNK_BYTES", 2):
                with self.assertLogs(
                        "storage_supabase", level="ERROR") as captured:
                    with self.assertRaisesRegex(OSError, "upload failure"):
                        storage_supabase.put("a" * 32, b"abcdef")

        self.assertIn("multipart upload", "\n".join(captured.output))

    def test_native_wrapper_rejects_invalid_pattern_buffer(self):
        import afc_native

        class RejectingLibrary:
            @staticmethod
            def segment_ids(*_args):
                return -1

        with mock.patch.object(afc_native, "_LIB", RejectingLibrary()):
            with self.assertRaisesRegex(ValueError, "rejected"):
                afc_native.segment_ids(b"abc", [b"ab"])

    def test_native_segment_kernel_rejects_truncated_pattern_buffer(self):
        import afc_native

        if not afc_native.AVAILABLE:
            self.skipTest("native backend is unavailable")
        outp = ctypes.c_void_p(123)
        outn = ctypes.c_uint32(99)
        result = afc_native._LIB.segment_ids(
            b"abc", 3, b"\x01\x00\x00", 3,
            ctypes.byref(outp), ctypes.byref(outn))
        self.assertEqual(result, -1)
        self.assertIsNone(outp.value)
        self.assertEqual(outn.value, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
