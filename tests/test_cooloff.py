"""Unit tests for the parts of cooloff.py that decide whether to apply a bump.

Every case here is drawn from a real sweep failure: the oxlint peer conflict
that took vue-demo#89 red, and the typescript ceiling that produced a bogus
downgrade recommendation for WorldCup.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "cooloff", os.path.join(os.path.dirname(__file__), "..", "scripts", "cooloff.py")
)
cooloff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cooloff)


class TestRangeSatisfies(unittest.TestCase):
    def test_tilde(self):
        # The exact constraint that broke vue-demo: ~1.82.0 excludes 1.83.0.
        self.assertTrue(cooloff.npm_range_satisfies("1.82.0", "~1.82.0"))
        self.assertTrue(cooloff.npm_range_satisfies("1.82.7", "~1.82.0"))
        self.assertFalse(cooloff.npm_range_satisfies("1.83.0", "~1.82.0"))
        self.assertFalse(cooloff.npm_range_satisfies("1.81.9", "~1.82.0"))

    def test_caret(self):
        self.assertTrue(cooloff.npm_range_satisfies("1.83.0", "^1.82.0"))
        self.assertFalse(cooloff.npm_range_satisfies("2.0.0", "^1.82.0"))
        # Caret on 0.x is major-like per semver.
        self.assertTrue(cooloff.npm_range_satisfies("0.2.9", "^0.2.3"))
        self.assertFalse(cooloff.npm_range_satisfies("0.3.0", "^0.2.3"))
        self.assertFalse(cooloff.npm_range_satisfies("0.0.4", "^0.0.3"))

    def test_comparators_and_unions(self):
        self.assertTrue(cooloff.npm_range_satisfies("6.0.5", ">=6.0 <6.1"))
        self.assertFalse(cooloff.npm_range_satisfies("6.1.0", ">=6.0 <6.1"))
        self.assertTrue(cooloff.npm_range_satisfies("7.0.0", "^6.0.0 || ^7.0.0"))
        self.assertTrue(cooloff.npm_range_satisfies("1.2.3", "*"))
        self.assertTrue(cooloff.npm_range_satisfies("1.2.9", "1.2.x"))
        self.assertFalse(cooloff.npm_range_satisfies("1.3.0", "1.2.x"))
        self.assertTrue(cooloff.npm_range_satisfies("2.0.0", "1.0.0 - 3.0.0"))

    def test_unknown_ranges_are_not_enforced(self):
        # None means "not understood" -- the caller must not block on it.
        self.assertIsNone(cooloff.npm_range_satisfies("1.0.0", "workspace:*"))
        self.assertIsNone(cooloff.npm_range_satisfies("1.0.0", "git+https://x/y"))
        self.assertIsNone(cooloff.npm_range_satisfies("not-a-version", "^1.0.0"))


class TestPeerCoherence(unittest.TestCase):
    def _rows(self, oxlint_target):
        return [
            {"ecosystem": "npm", "name": "eslint-plugin-oxlint", "current": "1.82.0",
             "target": "1.82.0", "changed": False, "status": "current"},
            {"ecosystem": "npm", "name": "oxlint", "current": "1.82.0",
             "target": oxlint_target, "changed": True, "status": "update"},
        ]

    def test_violating_bump_is_held_and_reverted(self):
        rows = self._rows("1.83.0")
        cooloff.npm_peer_deps = lambda n, v: (
            {"oxlint": "~1.82.0"} if n == "eslint-plugin-oxlint" else {}
        )
        cooloff.enforce_peer_coherence(rows)
        oxlint = rows[1]
        self.assertEqual(oxlint["status"], "peer_held")
        self.assertFalse(oxlint["changed"])
        # Reverted to the version that actually satisfies the peer.
        self.assertEqual(oxlint["target"], "1.82.0")
        self.assertEqual(oxlint["peer_conflicts"][0]["required_by"],
                         "eslint-plugin-oxlint")

    def test_satisfying_bump_is_left_alone(self):
        rows = self._rows("1.82.4")
        cooloff.npm_peer_deps = lambda n, v: (
            {"oxlint": "~1.82.0"} if n == "eslint-plugin-oxlint" else {}
        )
        cooloff.enforce_peer_coherence(rows)
        self.assertEqual(rows[1]["status"], "update")
        self.assertTrue(rows[1]["changed"])

    def test_peer_lookup_failure_never_sinks_the_sweep(self):
        rows = self._rows("1.83.0")

        def boom(n, v):
            raise RuntimeError("registry down")

        cooloff.npm_peer_deps = boom
        cooloff.enforce_peer_coherence(rows)
        self.assertEqual(rows[1]["status"], "update")


class TestCeilingScope(unittest.TestCase):
    def test_applies_only_when_the_breaking_toolchain_is_present(self):
        # WorldCup: plain tsc under Bun, no Angular CLI and no vue-tsc.
        c = cooloff.ceiling_for("npm", "typescript", {"typescript", "@types/bun"})
        self.assertFalse(c["applied"])
        # angular: the Angular CLI is installed, so the pin is real.
        c = cooloff.ceiling_for(
            "npm", "typescript", {"typescript", "@angular-devkit/build-angular"})
        self.assertTrue(c["applied"])
        self.assertEqual(c["max_major"], 6)
        self.assertIn("@angular-devkit/build-angular", c["matched"])

    def test_no_context_assumes_the_ceiling(self):
        c = cooloff.ceiling_for("npm", "typescript", None)
        self.assertTrue(c["applied"])
        self.assertEqual(c["context"], "assumed")

    def test_unceilinged_package_is_unaffected(self):
        self.assertIsNone(cooloff.ceiling_for("npm", "react", {"react"}))


class TestBackwardsGuard(unittest.TestCase):
    def test_detects_downgrade(self):
        self.assertTrue(cooloff._is_backwards("7.0.2", "6.0.3"))
        self.assertFalse(cooloff._is_backwards("6.0.3", "7.0.2"))
        self.assertFalse(cooloff._is_backwards("^7.0.2", "7.0.2"))
        # react-native's 1000.0.0 placeholder must not read as a real upgrade.
        self.assertTrue(cooloff._is_backwards("1000.0.0", "0.87.1"))
        self.assertFalse(cooloff._is_backwards(None, "1.0.0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
