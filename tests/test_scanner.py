import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghscan.config import Config
from ghscan.scanner import _parse_findings, _redact, build_command


def make_config(**overrides):
    base = dict(org="acme", github_token="dummy-token")
    base.update(overrides)
    return Config(**base)


class TestBuildCommand(unittest.TestCase):
    def test_default_command_includes_required_flags_in_order(self):
        cfg = make_config()
        cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertEqual(
            cmd[:5],
            ["trufflehog", "--no-update", "github", "--repo=https://github.com/acme/widgets", "--only-verified"],
        )

    def test_json_flag_added_by_default(self):
        cfg = make_config()
        cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertIn("--json", cmd)

    def test_json_flag_can_be_disabled(self):
        cfg = make_config(trufflehog_json_output=False)
        cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertNotIn("--json", cmd)

    def test_extra_args_appended(self):
        cfg = make_config(trufflehog_extra_args=["--concurrency=4"])
        cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertIn("--concurrency=4", cmd)

    def test_token_appended_when_present_in_env(self):
        cfg = make_config()
        with patch.dict("os.environ", {"GITHUB_TOKEN": "abc123"}):
            cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertIn("--token", cmd)
        self.assertIn("abc123", cmd)

    def test_token_not_appended_when_disabled(self):
        cfg = make_config(trufflehog_pass_token=False)
        with patch.dict("os.environ", {"GITHUB_TOKEN": "abc123"}):
            cmd = build_command(cfg, "https://github.com/acme/widgets")
        self.assertNotIn("--token", cmd)


class TestRedact(unittest.TestCase):
    def test_token_value_is_masked(self):
        cmd = ["trufflehog", "--token", "supersecret"]
        self.assertEqual(_redact(cmd), ["trufflehog", "--token", "***"])

    def test_no_token_present_is_a_noop(self):
        cmd = ["trufflehog", "--no-update"]
        self.assertEqual(_redact(cmd), cmd)


class TestParseFindings(unittest.TestCase):
    def test_parses_json_lines_and_ignores_noise(self):
        stdout = "\n".join([
            "trufflehog banner / progress text",
            '{"DetectorName": "AWS", "Verified": true}',
            "some other non-json line",
            '{"DetectorName": "Stripe", "Verified": true}',
        ])
        findings = _parse_findings(stdout, json_mode=True)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["DetectorName"], "AWS")

    def test_returns_empty_when_json_mode_disabled(self):
        self.assertEqual(_parse_findings('{"a": 1}', json_mode=False), [])

    def test_empty_stdout_yields_no_findings(self):
        self.assertEqual(_parse_findings("", json_mode=True), [])


if __name__ == "__main__":
    unittest.main()
