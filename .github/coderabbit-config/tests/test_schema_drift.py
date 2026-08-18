"""Unit tests for the vendored-schema drift detector."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema_drift  # noqa: E402

VENDORED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.v2.json"
)

BASE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tone_instructions": {"type": "string", "maxLength": 250},
        "reviews": {
            "type": "object",
            "properties": {"profile": {"type": "string"}},
        },
    },
}


def write(tmp, name, data, raw=None):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else json.dumps(data))
    return path


class LoadTest(unittest.TestCase):
    def test_redirect_stub_is_an_error_not_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", None, raw='<html><a href="/x">Moved</a></html>')
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            self.assertEqual(code, 2)
            self.assertIn("::error::", buf.getvalue())
            self.assertIn("curl -fsSL", buf.getvalue())

    def test_json_that_is_not_a_schema_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", {"hello": "world"})
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            self.assertEqual(code, 2)


class DriftTest(unittest.TestCase):
    def test_identical_schemas_report_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", BASE)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            self.assertEqual(code, 0)
            self.assertIn("No drift", buf.getvalue())

    def test_reformatting_alone_is_not_drift(self):
        # Byte-comparing would open a churn PR here; semantic comparison must not.
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", None, raw=json.dumps(BASE, indent=4, sort_keys=True))
            b = write(tmp, "b.json", None, raw=json.dumps(BASE, separators=(",", ":")))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            self.assertEqual(code, 0)

    def test_tightened_cap_is_flagged_first_and_loudly(self):
        tightened = json.loads(json.dumps(BASE))
        tightened["properties"]["tone_instructions"]["maxLength"] = 120
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", tightened)
            out = os.path.join(tmp, "summary.md")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(
                    ["--vendored", a, "--fetched", b, "--summary-out", out]
                )
            self.assertEqual(code, 1)
            with open(out, encoding="utf-8") as f:
                summary = f.read()
            self.assertIn("Tightened", summary.splitlines()[0])
            self.assertIn("tone_instructions: 250 → 120", summary)

    def test_added_and_removed_properties_are_listed_with_full_paths(self):
        changed = json.loads(json.dumps(BASE))
        changed["properties"]["reviews"]["properties"]["tools"] = {"type": "object"}
        del changed["properties"]["reviews"]["properties"]["profile"]
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", changed)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            summary = buf.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("`reviews.tools`", summary)
            self.assertIn("`reviews.profile`", summary)

    def test_description_only_drift_still_opens_a_pr_with_an_honest_summary(self):
        changed = json.loads(json.dumps(BASE))
        changed["properties"]["reviews"]["description"] = "new prose"
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", changed)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            self.assertEqual(code, 1)
            self.assertIn("Read the diff", buf.getvalue())


    def test_a_newly_capped_property_is_reported_as_tightened(self):
        # The most config-invalidating change upstream can make: a field that was
        # unbounded acquires a cap, so a config nobody touched is now rejected
        # WHOLE. The path is unchanged, so added/removed are empty — before the
        # union fix this printed "No property or maxLength changes".
        capped = json.loads(json.dumps(BASE))
        capped["properties"]["reviews"]["properties"]["profile"]["maxLength"] = 40
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", capped)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            summary = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Tightened", summary.splitlines()[0])
        self.assertIn("reviews.profile: uncapped → 40", summary)
        self.assertNotIn("the drift is elsewhere", summary)

    def test_a_removed_cap_is_reported_as_loosened(self):
        loosened = json.loads(json.dumps(BASE))
        del loosened["properties"]["tone_instructions"]["maxLength"]
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            b = write(tmp, "b.json", loosened)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(["--vendored", a, "--fetched", b])
            summary = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Loosened", summary)
        self.assertIn("tone_instructions: 250 → uncapped", summary)
        self.assertNotIn("the drift is elsewhere", summary)

    def test_caps_and_properties_inside_combinators_are_visible(self):
        # The vendored schema already uses both shapes (`filePatterns.items.anyOf`,
        # `mutually_exclusive_groups.additionalProperties`), so a walk that stops
        # at `properties`/dict-`items` is blind to drift the fleet would feel.
        nested = json.loads(json.dumps(BASE))
        nested["properties"]["reviews"]["properties"]["groups"] = {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"anyOf": [{"type": "string", "maxLength": 60}]},
            },
        }
        caps = schema_drift.length_caps(nested)
        paths = schema_drift.property_paths(nested)
        self.assertIn("reviews.groups", paths)
        self.assertEqual(
            caps["reviews.groups<additionalProperties>[]<anyOf[0]>"], 60
        )

    def test_an_unexpected_failure_exits_two_not_one(self):
        # Exit 1 means DRIFTED, and the caller force-resets a shared branch on it.
        # An unwritable --summary-out is not a verdict.
        with tempfile.TemporaryDirectory() as tmp:
            a = write(tmp, "a.json", BASE)
            changed = json.loads(json.dumps(BASE))
            changed["properties"]["reviews"]["description"] = "new prose"
            b = write(tmp, "b.json", changed)
            out_dir = os.path.join(tmp, "summary-is-a-dir")
            os.mkdir(out_dir)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = schema_drift.main(
                    ["--vendored", a, "--fetched", b, "--summary-out", out_dir]
                )
        self.assertEqual(code, 2)
        self.assertIn("could not compare", buf.getvalue())


class VendoredSchemaTest(unittest.TestCase):
    def test_the_committed_schema_loads_as_a_schema(self):
        # Guards the vendoring step itself: a truncated or stubbed commit of
        # schema.v2.json would make every consumer's check pass vacuously.
        schema = schema_drift.load(VENDORED, "vendored")
        self.assertIn("reviews", schema["properties"])
        self.assertIs(schema.get("additionalProperties"), False)

    def test_the_committed_schema_has_the_documented_cap_set(self):
        caps = schema_drift.length_caps(schema_drift.load(VENDORED, "vendored"))
        self.assertEqual(caps["tone_instructions"], 250)
        self.assertEqual(caps["reviews.path_instructions[].instructions"], 20000)
        self.assertEqual(len(caps), 14)


if __name__ == "__main__":
    unittest.main()
