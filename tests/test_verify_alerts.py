"""Unit tests for verify_alerts.py's range and lockfile parsing.

Cases come from the 2026-09-23 sweep: picomatch 2.3.2 was patched on its own
line while the 4.x floor was 4.0.4 (a floor comparison called it vulnerable),
and nested axios copies under adal-node survived a top-level axios bump.
"""
import os
import unittest
import importlib.util

spec = importlib.util.spec_from_file_location(
    "verify_alerts",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "verify_alerts.py"),
)
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)


class TestInRange(unittest.TestCase):
    def test_bounded(self):
        self.assertTrue(va.in_range("2.0.1", ">= 2.0.0, < 2.1.4"))
        self.assertFalse(va.in_range("2.1.4", ">= 2.0.0, < 2.1.4"))
        self.assertFalse(va.in_range("1.1.11", ">= 2.0.0, < 2.1.4"))

    def test_upper_only(self):
        self.assertTrue(va.in_range("1.1.11", "<= 1.1.11"))
        self.assertFalse(va.in_range("1.1.12", "<= 1.1.11"))

    def test_other_line_not_flagged(self):
        # picomatch 2.3.2 is patched; the 4.x advisory range must not catch it.
        self.assertFalse(va.in_range("2.3.2", ">= 4.0.0, < 4.0.4"))
        self.assertFalse(va.in_range("2.3.2", "< 2.3.2"))

    def test_exact_and_prerelease(self):
        self.assertTrue(va.in_range("1.0.0", "= 1.0.0"))
        self.assertTrue(va.in_range("2.0.0-beta.1", "< 2.0.0"))


class TestNpmLock(unittest.TestCase):
    def test_v3_nested_copies(self):
        lock = {"lockfileVersion": 3, "packages": {
            "": {"name": "app"},
            "node_modules/axios": {"version": "0.34.0"},
            "node_modules/adal-node/node_modules/axios": {"version": "0.21.4"},
        }}
        got = va.npm_versions(__import__("json").dumps(lock))
        self.assertEqual(got["axios"], {"0.34.0", "0.21.4"})

    def test_v1_nested_dependencies(self):
        lock = {"lockfileVersion": 1, "dependencies": {
            "filelist": {"version": "1.0.4", "dependencies": {
                "minimatch": {"version": "5.1.6"}}},
            "minimatch": {"version": "3.1.2"},
        }}
        got = va.npm_versions(__import__("json").dumps(lock))
        self.assertEqual(got["minimatch"], {"3.1.2", "5.1.6"})


class TestPnpmLock(unittest.TestCase):
    def test_v9(self):
        text = ("lockfileVersion: '9.0'\n"
                "importers:\n  .:\n    dependencies: {}\n"
                "packages:\n"
                "  '@img/sharp-linux-x64@0.35.4':\n    resolution: {}\n"
                "  nanoid@3.3.11:\n    resolution: {}\n"
                "snapshots:\n"
                "  next@15.5.26(react@19.0.0):\n    dependencies: {}\n")
        got = va.pnpm_versions(text)
        self.assertEqual(got["@img/sharp-linux-x64"], {"0.35.4"})
        self.assertEqual(got["nanoid"], {"3.3.11"})
        self.assertEqual(got["next"], {"15.5.26"})

    def test_v6_and_v5(self):
        v6 = "packages:\n  /glob@10.4.5:\n    resolution: {}\n  /@scope/pkg@1.2.3(peer@1.0.0):\n    x: 1\n"
        got = va.pnpm_versions(v6)
        self.assertEqual(got["glob"], {"10.4.5"})
        self.assertEqual(got["@scope/pkg"], {"1.2.3"})
        v5 = "packages:\n  /glob/10.4.5:\n    x: 1\n  /@scope/pkg/1.2.3_peer@1.0.0:\n    x: 1\n"
        got = va.pnpm_versions(v5)
        self.assertEqual(got["glob"], {"10.4.5"})
        self.assertEqual(got["@scope/pkg"], {"1.2.3"})


class TestTarballMismatch(unittest.TestCase):
    def test_hand_edited_version(self):
        # node1 #3: version bumped, resolved/integrity left on the old tarball.
        lock = {"lockfileVersion": 3, "packages": {"node_modules/path-to-regexp": {
            "version": "0.1.13",
            "resolved": "https://registry.npmjs.org/path-to-regexp/-/path-to-regexp-0.1.12.tgz"}}}
        got = va.npm_mismatches(__import__("json").dumps(lock))
        self.assertEqual(got, {"path-to-regexp": ["0.1.13 (tarball 0.1.12)"]})

    def test_consistent_and_scoped(self):
        lock = {"lockfileVersion": 3, "packages": {
            "node_modules/@babel/core": {"version": "7.29.7",
                "resolved": "https://registry.npmjs.org/@babel/core/-/core-7.29.7.tgz"},
            "node_modules/x": {"version": "1.0.0-beta.2",
                "resolved": "https://registry.npmjs.org/x/-/x-1.0.0-beta.2.tgz"}}}
        self.assertEqual(va.npm_mismatches(__import__("json").dumps(lock)), {})


if __name__ == "__main__":
    unittest.main()
