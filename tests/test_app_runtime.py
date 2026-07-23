import unittest
from unittest.mock import MagicMock, patch

import app_runtime


class UpdateSchedulingTests(unittest.TestCase):
    def test_first_check_is_due_immediately(self):
        self.assertEqual(
            app_runtime.seconds_until_next_update_check(0, now=1_000),
            0,
        )

    def test_next_check_is_due_once_per_day(self):
        half_day = app_runtime.UPDATE_CHECK_INTERVAL_SECONDS / 2
        self.assertEqual(
            app_runtime.seconds_until_next_update_check(
                1_000,
                now=1_000 + half_day,
            ),
            half_day,
        )


class GitHubReleaseTests(unittest.TestCase):
    def _release_response(self, digest):
        response = MagicMock()
        response.json.return_value = {
            "tag_name": "1.0.7",
            "assets": [
                {
                    "name": "FreeWhisper.dmg",
                    "browser_download_url": "https://example.test/FreeWhisper.dmg",
                    "digest": digest,
                }
            ],
        }
        return response

    @patch("app_runtime.get_current_version", return_value="1.0.6")
    @patch("requests.get")
    def test_update_includes_github_sha256(self, get, _current_version):
        digest = "a" * 64
        get.return_value = self._release_response(f"sha256:{digest}")

        self.assertEqual(
            app_runtime.check_for_update(),
            ("1.0.7", "https://example.test/FreeWhisper.dmg", digest),
        )

    @patch("app_runtime.get_current_version", return_value="1.0.6")
    @patch("requests.get")
    def test_update_without_digest_is_rejected(self, get, _current_version):
        get.return_value = self._release_response(None)

        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            app_runtime.check_for_update()

    @patch("app_runtime.canonical_app_bundle_path", return_value="/Applications/FreeWhisper.app")
    @patch("requests.get")
    def test_download_with_wrong_digest_stops_before_install(self, get, _bundle_path):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"not the expected update"]
        get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "SHA-256 verification"):
            app_runtime.download_and_apply_update(
                "https://example.test/FreeWhisper.dmg",
                "0" * 64,
                relaunch=False,
            )


if __name__ == "__main__":
    unittest.main()
