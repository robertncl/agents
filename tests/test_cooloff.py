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

    def test_skip_majors_leaves_a_major_for_its_own_pr(self):
        # gh-action-demo: download-provisioning-profiles v6.1.0 -> v7.0.0 had
        # to be filtered out of batch.json by hand to keep it out of a patch PR.
        rows = [
            {"kind": "action", "repo": "actions/checkout", "tag": "v5.0.1",
             "sha": self.NEW, "status": "update"},
            {"kind": "action", "repo": "github/codeql-action", "tag": "v4.2.0",
             "sha": self.NEW, "status": "update"},
        ]
        changes, _, left = cooloff.plan_pins(self.root, rows, skip_majors=True)
        self.assertEqual([c["uses"].split("@")[0] for c in changes], ["github/codeql-action/init"])
        majors = [f for f in left if f.get("major_to")]
        self.assertEqual([(f["uses"], f["major_to"]) for f in majors],
                         [("actions/checkout@v4", "v5.0.1")])
        # Without the flag the major is applied, as before.
        changes, _, _ = cooloff.plan_pins(self.root, rows)
        self.assertEqual(len(changes), 2)


class TestRowShape(unittest.TestCase):
    """vue-demo, 2026-10-14: `r['name']` raised KeyError at the first action
    row, the agent then printed only non-update rows, never saw the
    scorecard-action bump, and its PR said "No actions to pin"."""

    def setUp(self):
        self.saved = (cooloff.resolve_action, cooloff.resolve_pkg)

    def tearDown(self):
        cooloff.resolve_action, cooloff.resolve_pkg = self.saved

    def test_action_row_carries_package_keys(self):
        cooloff.resolve_action = lambda *a, **k: {
            "kind": "action", "repo": "ossf/scorecard-action", "current": "v2.4.4",
            "tag": "v2.5.0", "sha": "f" * 40, "age_hours": 57.0, "changed": True,
            "skipped_too_new": []}
        r = cooloff.resolve_spec("action:ossf/scorecard-action@v2.4.4", 24, False, False, 10)
        self.assertEqual((r["ecosystem"], r["name"], r["current"], r["target"], r["status"]),
                         ("action", "ossf/scorecard-action", "v2.4.4", "v2.5.0", "update"))

    def test_error_and_held_back_rows_carry_identity(self):
        def boom(*a, **k):
            raise cooloff.ResolveError("x: HTTP 404")
        cooloff.resolve_action = boom
        r = cooloff.resolve_spec("action:veracode/veracode-security-scan@v1", 24, False, False, 10)
        self.assertEqual((r["status"], r["ecosystem"], r["name"], r["current"]),
                         ("error", "action", "veracode/veracode-security-scan", "v1"))

        def held(*a, **k):
            raise cooloff.ResolveError("new: none old enough", held_back=[{"version": "1.0.0"}])
        cooloff.resolve_pkg = held
        r = cooloff.resolve_spec("npm:brand-new@0.9.0", 24, False, False, 10)
        self.assertEqual((r["status"], r["ecosystem"], r["name"], r["current"], r["target"]),
                         ("held_back", "npm", "brand-new", "0.9.0", None))

        r = cooloff.resolve_spec("../transitive.txt", 24, False, False, 10)
        self.assertEqual((r["status"], r["name"]), ("error", "../transitive.txt"))


class TestBatchInput(unittest.TestCase):
    def test_a_spec_file_argument_is_read_not_treated_as_a_spec(self):
        # 29 past runs ran `batch ../tr.txt` and got one `bad spec` row back.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("npm:vitest@5.0.4\n# comment\n\nnpm:obug@3.0.0\n")
        try:
            specs, read = cooloff._read_specs([fh.name, "pypi:ruff@0.17.0"])
        finally:
            os.unlink(fh.name)
        self.assertTrue(read)
        self.assertEqual([s for s in specs if s and not s.startswith("#")],
                         ["npm:vitest@5.0.4", "npm:obug@3.0.0", "pypi:ruff@0.17.0"])


class TestHeldBackReporting(unittest.TestCase):
    """closeclaw#30 and vue-demo#112 both said "held back: none" while
    lucide-react 1.57.0, pinia 4.1.0 and eslint-plugin-oxlint 1.88.0 sat
    inside the window -- on `update` and `current` rows, not `held_back` ones."""

    def test_select_reports_hours_short(self):
        from datetime import datetime, timedelta, timezone
        t = datetime.now(timezone.utc)
        chosen, skipped, _ = cooloff.select(
            [("1.56.0", t - timedelta(hours=30)), ("1.57.0", t - timedelta(hours=5.5))],
            24, False)
        self.assertEqual(chosen["version"], "1.56.0")
        self.assertEqual(skipped[0]["version"], "1.57.0")
        self.assertAlmostEqual(skipped[0]["hours_short"], 18.5, places=1)

    def test_current_row_with_a_waiting_release_needs_attention(self):
        quiet = {"status": "current", "spec": "npm:vue@3.5.44", "skipped_too_new": []}
        waiting = {"status": "current", "spec": "npm:pinia@4.0.3",
                   "skipped_too_new": [{"version": "4.1.0", "hours_short": 21.6}]}
        self.assertFalse(cooloff._needs_attention(quiet))
        self.assertTrue(cooloff._needs_attention(waiting))
        line = cooloff._text_line(waiting, 16)
        self.assertIn("up to date; held back: 4.1.0 (21.6h short)", line)

    def test_out_writes_every_row_and_prints_only_the_work_list(self):
        import argparse
        import contextlib
        import io
        import json
        import tempfile
        rows = {
            "npm:vue@3.5.43": {"kind": "pkg", "ecosystem": "npm", "name": "vue",
                               "current": "3.5.43", "target": "3.5.44", "age_hours": 28.0,
                               "changed": True, "skipped_too_new": []},
            "npm:react@19.3.0": {"kind": "pkg", "ecosystem": "npm", "name": "react",
                                 "current": "19.3.0", "target": "19.3.0", "age_hours": 900.0,
                                 "changed": False, "skipped_too_new": []},
        }
        saved = cooloff.resolve_pkg, cooloff.npm_peer_deps
        cooloff.resolve_pkg = lambda eco, name, cur, *a, **k: dict(rows[f"{eco}:{name}@{cur}"])
        cooloff.npm_peer_deps = lambda name, version: {}  # no registry calls from a unit test
        out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        args = argparse.Namespace(specs=list(rows), out=out, json=False, hours=24,
                                  allow_prerelease=False, same_major=False, jobs=2, max_probe=10)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cooloff.cmd_batch(args)
            saved_rows = json.load(open(out))
        finally:
            cooloff.resolve_pkg, cooloff.npm_peer_deps = saved
            os.unlink(out)
        self.assertEqual([r["status"] for r in saved_rows], ["update", "current"])
        text = buf.getvalue()
        self.assertIn("UPDATE     npm:vue@3.5.43", text)
        self.assertNotIn("react", text)
        self.assertIn("1 current, 1 update -- every row in", text)


class TestCheckAge(unittest.TestCase):
    """The transitive check piped `pkg@locked` through `batch`, which reports
    the newest *cleared* version: pinia 4.1.0, two hours old, came back
    `ahead` with "1585h old" (4.0.3's age) and passed a check for `held_back`."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        t = datetime.now(timezone.utc)
        self.saved = cooloff.FETCHERS["npm"], cooloff.versions_npm
        fake = lambda name, allow_prerelease=False: [  # noqa: E731
            ("4.0.3", t - timedelta(hours=1585)), ("4.1.0", t - timedelta(hours=2.4))]
        cooloff.FETCHERS["npm"] = cooloff.versions_npm = fake

    def tearDown(self):
        cooloff.FETCHERS["npm"], cooloff.versions_npm = self.saved

    def test_exact_version_too_new_is_flagged(self):
        r = cooloff.check_age("npm:pinia@4.1.0", 24)
        self.assertEqual(r["status"], "too_new")
        self.assertAlmostEqual(r["hours_short"], 21.6, places=1)
        self.assertEqual(cooloff.check_age("npm:pinia@4.0.3", 24)["status"], "ok")

    def test_unlisted_version_is_unknown_not_ok(self):
        self.assertEqual(cooloff.check_age("npm:pinia@9.9.9", 24)["status"], "unknown")
        self.assertEqual(cooloff.check_age("npm:pinia", 24)["status"], "error")

    def test_ahead_row_does_not_carry_the_declined_versions_age(self):
        r = cooloff.resolve_spec("npm:pinia@4.1.0", 24, False, False, 10)
        self.assertEqual((r["status"], r["target"], r["age_hours"]), ("ahead", "4.1.0", None))
        self.assertEqual(r["downgrade_declined"]["to"], "4.0.3")


class TestMavenVersions(unittest.TestCase):
    """payment, 2026-10-14: search.maven.org's index stopped at
    spotless-maven-plugin 2.44.5 while 3.10.4 was live, so the 3.10.3 pin read
    as `ahead` -- unless the search happened to time out that run."""

    def setUp(self):
        from datetime import datetime, timezone
        self.old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.saved = (cooloff._maven_index_dates, cooloff._maven_metadata_versions,
                      cooloff._maven_pom_date)
        self.probed = []

        def pom_date(g, a, v):
            self.probed.append(v)
            return self.old
        cooloff._maven_pom_date = pom_date

    def tearDown(self):
        (cooloff._maven_index_dates, cooloff._maven_metadata_versions,
         cooloff._maven_pom_date) = self.saved

    def test_metadata_decides_what_exists_and_stale_index_only_dates(self):
        cooloff._maven_index_dates = lambda g, a: {"2.44.4": self.old, "2.44.5": self.old}
        cooloff._maven_metadata_versions = lambda g, a: ["2.44.4", "2.44.5", "3.10.3", "3.10.4"]
        got = dict(cooloff.versions_maven("com.diffplug.spotless:spotless-maven-plugin"))
        self.assertEqual(set(got), {"2.44.4", "2.44.5", "3.10.3", "3.10.4"})
        self.assertEqual(sorted(self.probed), ["3.10.3", "3.10.4"])  # only what the index lacked

    def test_index_down_still_dates_older_major_lines(self):
        # --same-major on 2.x with the index down found nothing dated at all.
        cooloff._maven_index_dates = lambda g, a: {}
        three = [f"3.{i}.0" for i in range(20)]
        cooloff._maven_metadata_versions = lambda g, a: ["2.45.0", "2.46.0", "2.46.1"] + three
        got = dict(cooloff.versions_maven("g:a", probe=15))
        self.assertIn("2.46.1", got)
        self.assertEqual(len([v for v in got if v.startswith("3.")]), 15)


class TestTransientRetry(unittest.TestCase):
    def setUp(self):
        self.saved = (cooloff.urllib.request.urlopen, cooloff.time.sleep)
        cooloff.time.sleep = lambda s: None

    def tearDown(self):
        cooloff.urllib.request.urlopen, cooloff.time.sleep = self.saved

    def test_truncated_body_is_retried_once(self):
        # nextjs: eslint-config-next's 7 MB packument arrived truncated.
        import http.client
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                calls.append(1)
                if len(calls) == 1:
                    raise http.client.IncompleteRead(b"{", 10)
                return b'{"ok": true}'

        cooloff.urllib.request.urlopen = lambda req, timeout=30: Resp()
        self.assertEqual(cooloff._read(object()), b'{"ok": true}')
        self.assertEqual(len(calls), 2)

    def test_a_404_is_an_answer_not_retried(self):
        calls = []

        def nope(req, timeout=30):
            calls.append(1)
            raise cooloff.urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        cooloff.urllib.request.urlopen = nope
        with self.assertRaises(cooloff.urllib.error.HTTPError):
            cooloff._read(object())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
