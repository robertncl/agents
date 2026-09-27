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

    def test_applies_for_next_js_via_eslint_config_next(self):
        # nextjs: no Angular or Vue tooling at all, but eslint-config-next
        # pulls in typescript-eslint, which pins its own peerDependency of
        # typescript ">=4.8.4 <6.1.0". Confirmed breaking `npm run lint`
        # with TS7 on 2026-09-16 even though `npm run build` passed clean.
        c = cooloff.ceiling_for(
            "npm", "typescript", {"typescript", "next", "eslint-config-next"})
        self.assertTrue(c["applied"])
        self.assertEqual(c["max_major"], 6)
        self.assertIn("eslint-config-next", c["matched"])
        # bun-app: plain typescript devDependency, no eslint-config-next and
        # no Angular/Vue tooling -- the ceiling must not apply here.
        c = cooloff.ceiling_for(
            "npm", "typescript", {"typescript", "playwright", "bun"})
        self.assertFalse(c["applied"])

    def test_no_context_assumes_the_ceiling(self):
        c = cooloff.ceiling_for("npm", "typescript", None)
        self.assertTrue(c["applied"])
        self.assertEqual(c["context"], "assumed")

    def test_unceilinged_package_is_unaffected(self):
        self.assertIsNone(cooloff.ceiling_for("npm", "react", {"react"}))

    def test_inert_ceiling_is_not_reported_on_the_row(self):
        # WorldCup and bun-app are on TypeScript 7 with no trigger package.
        # A row still carrying the ceiling got reported as "held back at
        # major 6" for a repo that was never held back.
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(days=60)
        saved = cooloff.FETCHERS["npm"]
        cooloff.FETCHERS["npm"] = lambda name, allow_prerelease=False: [("7.0.2", old)]
        try:
            r = cooloff.resolve_pkg("npm", "typescript", "7.0.2",
                                    context_names={"typescript", "@types/bun"})
        finally:
            cooloff.FETCHERS["npm"] = saved
        self.assertIsNone(r["ceiling"])
        self.assertFalse(r["changed"])


class TestNpmLatestCap(unittest.TestCase):
    """pokedev: Expo's next-SDK modules ship plain version numbers under the
    `next` dist-tag. Only `latest` says what npm considers released."""

    def setUp(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.cands = [("57.0.15", ts), ("57.0.18", ts), ("58.0.7", ts),
                      ("58.0.0-canary-20260909-ea7a89a", ts)]

    def test_versions_above_latest_are_dropped(self):
        out = cooloff.npm_cap_at_latest(
            self.cands, {"latest": "57.0.18", "next": "58.0.7"})
        self.assertEqual([v for v, _ in out], ["57.0.15", "57.0.18"])

    def test_missing_or_prerelease_latest_caps_nothing(self):
        self.assertEqual(cooloff.npm_cap_at_latest(self.cands, {}), self.cands)
        self.assertEqual(
            cooloff.npm_cap_at_latest(self.cands, {"latest": "1.0.0-rc.1"}), self.cands)


class TestMavenPrerelease(unittest.TestCase):
    def test_milestones_and_candidates_are_not_stable(self):
        # spring-boot-starter-* 4.2.0-M2 read as the newest stable release.
        for v in ("4.2.0-M2", "5.13.0-M3", "1.0.0-CR1", "1.0.0-RC2"):
            self.assertFalse(cooloff.is_stable(v), v)
        for v in ("3.5.3", "2.6.0", "6.5.6"):
            self.assertTrue(cooloff.is_stable(v), v)


class TestPomScan(unittest.TestCase):
    """payment: every pin sat behind a property or in <parent>/<plugin>, so
    the old scan found nothing it could resolve."""

    POM = """<project>
  <parent>
    <groupId>com.alipay.sofa</groupId>
    <artifactId>sofaboot-dependencies</artifactId>
    <version>4.6.0</version>
  </parent>
  <groupId>io.paylab</groupId>
  <version>0.1.0-SNAPSHOT</version>
  <properties>
    <seata.version>2.6.0</seata.version>
    <spotless.version>3.10.2</spotless.version>
  </properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>io.paylab</groupId><artifactId>paylab-common</artifactId>
      <version>${project.version}</version></dependency>
    <dependency><groupId>org.apache.seata</groupId><artifactId>seata-sofa-rpc</artifactId>
      <version>${seata.version}</version></dependency>
    <!-- <dependency><groupId>x</groupId><artifactId>commented</artifactId><version>1</version></dependency> -->
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>com.alipay.sofa</groupId><artifactId>rpc-sofa-boot-starter</artifactId></dependency>
    <dependency><groupId>a.b</groupId><artifactId>undefined-prop</artifactId>
      <version>${nowhere.version}</version></dependency>
  </dependencies>
  <build><plugins>
    <plugin><groupId>com.diffplug.spotless</groupId><artifactId>spotless-maven-plugin</artifactId>
      <version>${spotless.version}</version>
      <configuration><java><version>9.9</version></java></configuration></plugin>
    <plugin><artifactId>maven-surefire-plugin</artifactId></plugin>
    <plugin><artifactId>maven-jar-plugin</artifactId><version>3.4.2</version></plugin>
  </plugins></build>
</project>"""

    def test_properties_parent_and_plugins(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "pom.xml")
        with open(path, "w") as f:
            f.write(self.POM)
        out, notes = [], []
        cooloff._deps_pom(path, d, out, notes)
        got = {r["name"]: r["current"] for r in out}
        self.assertEqual(got, {
            "com.alipay.sofa:sofaboot-dependencies": "4.6.0",
            "org.apache.seata:seata-sofa-rpc": "2.6.0",
            "com.diffplug.spotless:spotless-maven-plugin": "3.10.2",
            "org.apache.maven.plugins:maven-jar-plugin": "3.4.2",
        })
        # Undefined property: reported for a hand check, never guessed.
        self.assertEqual(len(notes), 1)
        self.assertIn("${nowhere.version}", notes[0])


class TestVersionEquality(unittest.TestCase):
    """Go pins carry a `v`; npm pins carry range operators. Neither is a change."""

    def test_go_v_prefix_is_not_a_change(self):
        # crypto's whole go.mod reported as "update v5.3.2 -> v5.3.2".
        self.assertTrue(cooloff.same_version("v5.3.2", "v5.3.2"))
        self.assertTrue(cooloff.same_version("v5.3.2", "5.3.2"))
        self.assertTrue(cooloff.same_version("5.3.2", "v5.3.2"))

    def test_range_operators_are_stripped(self):
        self.assertTrue(cooloff.same_version("^19.3.0", "19.3.0"))
        self.assertTrue(cooloff.same_version("~1.82.0", "1.82.0"))
        self.assertTrue(cooloff.same_version(">=4.11.8", "4.11.8"))

    def test_real_differences_still_register(self):
        self.assertFalse(cooloff.same_version("v5.3.2", "v5.4.0"))
        self.assertFalse(cooloff.same_version("^19.3.0", "19.4.0"))
        self.assertFalse(cooloff.same_version("8.2.2", "8.3.0"))

    def test_unparseable_falls_back_to_string_equality(self):
        self.assertTrue(cooloff.same_version("main", "main"))
        self.assertFalse(cooloff.same_version("main", "v1.0.0"))
        self.assertFalse(cooloff.same_version(None, "1.0.0"))

    def test_padding_does_not_collapse_distinct_versions(self):
        self.assertTrue(cooloff.same_version("1.2", "1.2.0"))
        self.assertFalse(cooloff.same_version("1.2", "1.2.1"))


class TestBackwardsGuard(unittest.TestCase):
    def test_detects_downgrade(self):
        self.assertTrue(cooloff._is_backwards("7.0.2", "6.0.3"))
        self.assertFalse(cooloff._is_backwards("6.0.3", "7.0.2"))
        self.assertFalse(cooloff._is_backwards("^7.0.2", "7.0.2"))
        # react-native's 1000.0.0 placeholder must not read as a real upgrade.
        self.assertTrue(cooloff._is_backwards("1000.0.0", "0.87.1"))
        self.assertFalse(cooloff._is_backwards(None, "1.0.0"))


class TestPinActions(unittest.TestCase):
    """Sweeps spent ~190 Read/Edit calls hand-rewriting `uses:` lines."""

    OLD = "a" * 40
    NEW = "b" * 40

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        wf = os.path.join(self.root, ".github", "workflows")
        os.makedirs(wf)
        self.path = os.path.join(wf, "ci.yml")
        with open(self.path, "w") as fh:
            fh.write(
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                f"      - uses: github/codeql-action/init@{self.OLD} # v4.1.0\n"
                "      - uses: ./local-action\n"
                "      - name: held\n"
                "        uses: 'docker/login-action@v3'\n"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [
            {"kind": "action", "repo": "actions/checkout", "tag": "v5.0.1",
             "sha": self.NEW, "status": "update"},
            {"kind": "action", "repo": "github/codeql-action", "tag": "v4.2.0",
             "sha": self.NEW, "status": "update"},
            {"spec": "action:docker/login-action@v3", "status": "held_back"},
        ]

    def test_rewrites_and_keeps_subpath(self):
        changes, mismatches, left = cooloff.plan_pins(self.root, self.rows())
        cooloff.apply_pins(self.root, changes)
        text = open(self.path).read()
        self.assertIn(f"      - uses: actions/checkout@{self.NEW} # v5.0.1\n", text)
        self.assertIn(f"      - uses: github/codeql-action/init@{self.NEW} # v4.2.0\n", text)
        self.assertIn("./local-action", text)
        self.assertEqual(mismatches, [])
        # The held-back action stays on its tag and is surfaced, not dropped.
        self.assertEqual([f["uses"] for f in left], ["docker/login-action@v3"])
        self.assertIn("uses: 'docker/login-action@v3'", text)

    def test_already_current_is_noop(self):
        rows = [{"kind": "action", "repo": "github/codeql-action", "tag": "v4.1.0",
                 "sha": self.OLD, "status": "current"}]
        changes, mismatches, _ = cooloff.plan_pins(self.root, rows)
        self.assertEqual([c for c in changes if "codeql" in c["uses"]], [])
        self.assertEqual(mismatches, [])

    def test_moved_tag_is_mismatch_not_update(self):
        rows = [{"kind": "action", "repo": "github/codeql-action", "tag": "v4.1.0",
                 "sha": self.NEW, "status": "current"}]
        changes, mismatches, _ = cooloff.plan_pins(self.root, rows)
        self.assertEqual(changes, [])
        self.assertEqual(len(mismatches), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
