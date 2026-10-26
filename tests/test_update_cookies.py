"""Check the BAT's PowerShell command without reading cookies or contacting GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell required")
class UpdateCookiesTests(unittest.TestCase):
    def test_validation_and_upload_exit_code(self):
        batch = (Path(__file__).resolve().parents[1] / "update_cookies.bat").read_text(
            encoding="utf-8"
        )
        command = next(
            line for line in batch.splitlines()
            if line.startswith('powershell -NoProfile -Command "')
        ).split(' -Command "', 1)[1][:-1]
        cases = [
            ("", 0, False),
            ("unrelated clipboard text", 0, False),
            ('[{"name":"cna","value":"example"}]', 0, False),
            ("other=value", 0, False),
            ("cna=example\nother=value", 0, False),
            ("cna=example; xman_us_f=test==", 0, True),
            ("Cookie: cna=example;", 0, True),
            ("cna=example", 7, True),
        ]
        for clipboard, gh_exit, should_upload in cases:
            with self.subTest(clipboard=clipboard, gh_exit=gh_exit):
                # Functions shadow the real clipboard and GitHub CLI commands.
                stub = (
                    "function Get-Clipboard { param([switch]$Raw) "
                    "return $env:TEST_COOKIE_INPUT }; "
                    "function gh { $received = @($input); "
                    "if ($args[0] -ne 'secret' -or $args[1] -ne 'set' "
                    "-or $args[2] -ne $env:SECRET_NAME "
                    "-or $args[3] -ne '--repo' -or $args[4] -ne 'Danagar42/Ali' "
                    "-or $received.Count -ne 1) { throw 'Invalid upload' }; "
                    "Write-Output 'MOCK_UPLOAD'; "
                    f"$global:LASTEXITCODE = {gh_exit} }}; "
                )
                for secret in ("ALI_COOKIES", "ALI_COOKIES_1"):
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", stub + command],
                        env={**os.environ, "TEST_COOKIE_INPUT": clipboard, "SECRET_NAME": secret},
                        capture_output=True,
                        timeout=20,
                    )
                    self.assertEqual(b"MOCK_UPLOAD" in result.stdout, should_upload)
                    self.assertEqual(result.returncode, gh_exit if should_upload else 1)


if __name__ == "__main__":
    unittest.main()
