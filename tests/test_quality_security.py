"""Behavioral regression for encoded secrets and every config write entry."""
import json
import os
import tempfile
from unittest import mock

import sf_config
import sf_diag
import sf_logging
import sf_privacy
from tests._base import SFCase


def internal_ledger():
    name = "_INTERNAL_FAULTS"
    return getattr(sf_logging, name)


def write_json(path, payload):
    name = "_atomic_write_json"
    return getattr(sf_config, name)(path, payload)


class QualitySecurityTests(SFCase):
    def test_encoded_query_names_never_export_the_secret(self):
        for key in ("token", "%74%6f%6b%65%6e", "%2574oken", "API%5fKEY", "access_token"):
            with self.subTest(key=key):
                url = "https://example.invalid/q?code=%s&" + key + "=PRIVATE-CANARY"
                config = sf_config.redact_config({"api_urls": {"quote": url}})
                self.assertNotIn("PRIVATE-CANARY", json.dumps(config))
                self.assertIn("%s", config["api_urls"]["quote"])
                self.assertNotIn("PRIVATE-CANARY", sf_diag.scrub_text(url, ()))

    def test_userinfo_fragment_and_duplicate_secret_parameters(self):
        value = sf_privacy.redact_url(
            "https://user:PRIVATE-CANARY@example.invalid/q?token=PRIVATE-CANARY&token=PRIVATE-CANARY#access_token=PRIVATE-CANARY")
        self.assertNotIn("PRIVATE-CANARY", value)
        self.assertNotIn("user:", value)

    def test_benign_url_and_template_stay_unchanged(self):
        value = "https://example.invalid/q?code=%s&fields=f1,f2&name=a+b"
        self.assertEqual(sf_privacy.redact_url(value), value)
        self.assertEqual(sf_privacy.redact_url("not a URL"), "not a URL")

    def test_malformed_url_is_not_repeated(self):
        self.assertEqual(sf_privacy.redact_url("https://[bad?token=PRIVATE-CANARY"), "***REDACTED***")

    def test_local_log_hides_secrets_before_both_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "debug.log")
            with mock.patch.object(sf_logging, "LOG_PATH", path), \
                    mock.patch.object(sf_logging, "_nslog_line") as system_log:
                sf_logging.log_line("https://example.invalid/?%74oken=PRIVATE-CANARY", "WARNING")
                with open(path, encoding="utf-8") as stream:
                    self.assertNotIn("PRIVATE-CANARY", stream.read())
                self.assertNotIn("PRIVATE-CANARY", system_log.call_args.args[0])

    def test_logging_failure_is_observable_without_recursive_logging(self):
        previous = sf_logging.internal_faults()
        count = previous.get("file_write", 0)
        with mock.patch.dict(internal_ledger(), previous, clear=True), \
                mock.patch("builtins.open", side_effect=PermissionError("denied")), \
                mock.patch.object(sf_logging, "_maybe_rotate_log"):
            sf_logging.log_line("sample")
            self.assertEqual(sf_logging.internal_faults().get("file_write"), count + 1)

    def test_face_guard_cannot_write_future_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            write_json(path, {"version": sf_config.CONFIG_SCHEMA_VERSION + 1,
                              "face_guard": False})
            with mock.patch.object(sf_config, "CONFIG_PATH", path), \
                    mock.patch.object(sf_config, "CONFIG_BACKUP_PATH", path + ".bak"):
                self.assertFalse(sf_config.save_face_guard_pref(True))
            with open(path, encoding="utf-8") as stream:
                self.assertFalse(json.load(stream)["face_guard"])
