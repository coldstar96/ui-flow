import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import journal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.channel = tempfile.mkdtemp(prefix="uiflow-journal-")
        self.addCleanup(shutil.rmtree, self.channel, True)
        self.state = tempfile.mkdtemp(prefix="uiflow-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        saved = journal.paths.STATE_DIR
        journal.paths.STATE_DIR = self.state
        self.addCleanup(setattr, journal.paths, "STATE_DIR", saved)

    def append(self, *entries):
        with open(os.path.join(self.channel, journal.JOURNAL_NAME), "a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def step(self, op="tap", ok=True, **extra):
        entry = {"ts": 1.0, "seq": 1, "op": op, "ms": 12, "ok": ok}
        entry.update(extra)
        return entry

    def test_since_mark_returns_only_new_entries(self):
        self.append(self.step(sel={"label": "old"}))
        journal.mark(self.channel)
        self.append(self.step(sel={"label": "new"}))
        entries = journal.since_mark(self.channel)
        self.assertEqual([e["sel"]["label"] for e in entries], ["new"])

    def test_a_partial_trailing_line_is_not_consumed(self):
        self.append(self.step(sel={"label": "a"}))
        with open(os.path.join(self.channel, journal.JOURNAL_NAME), "a") as handle:
            handle.write('{"op": "tap", "se')
        entries, offset = journal.read(self.channel, 0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(offset, len(json.dumps(self.step(sel={"label": "a"}), ensure_ascii=False)) + 1)

    def test_a_mark_from_a_different_channel_resets_to_zero(self):
        self.append(self.step(sel={"label": "a"}), self.step(sel={"label": "b"}))
        journal.write_mark("/some/other/channel", 999999)
        entries = journal.since_mark(self.channel)
        self.assertEqual(len(entries), 2)
        self.assertTrue(journal.mark_is_stale(self.channel))

    def test_a_mark_past_the_end_after_rotation_resets_to_zero(self):
        self.append(self.step(sel={"label": "a"}))
        journal.write_mark(self.channel, 10_000_000)
        self.assertEqual(len(journal.since_mark(self.channel)), 1)

    def test_scaffolding_and_failures_are_dropped(self):
        entries = [
            self.step(op="ping"),
            self.step(op="quit"),
            self.step(op="snapshot"),
            self.step(op="screenshot"),
            self.step(op="sleep", ms=200),
            self.step(op="tap", ok=False, sel={"label": "missed"}),
            self.step(op="tap", sel={"label": "kept"}),
        ]
        steps = journal.to_steps(entries)
        self.assertEqual([s["op"] for s in steps], ["tap"])
        self.assertEqual(steps[0]["sel"], {"label": "kept"})

    def test_meta_keys_are_stripped_from_a_step(self):
        steps = journal.to_steps([self.step(op="tap", sel={"label": "a"}, info="cpm=900")])
        self.assertEqual(steps[0], {"op": "tap", "sel": {"label": "a"}})

    def test_secure_text_is_journaled_as_a_length_never_as_text(self):
        entry = self.step(op="type", text="hunter2", sel={"type": "secureTextField"})
        steps = journal.to_steps([entry])
        self.assertNotIn("text", steps[0])
        self.assertEqual(steps[0]["textLen"], 7)

    def test_plain_text_survives_but_is_reported_as_literal(self):
        steps = journal.to_steps([self.step(op="type", text="hello", sel={"type": "textField"})])
        self.assertEqual(steps[0]["text"], "hello")
        self.assertEqual(journal.literal_text_steps(steps), [0])

    def test_to_record_carries_every_schema_key(self):
        record = journal.to_record(
            "com.example.Demo",
            "open-settings",
            [{"op": "tap", "sel": {"label": "a"}}],
            description="opens settings",
            aliases=["open settings"],
            requires=["launch"],
            satisfied_when=[{"op": "assert", "sel": {"label": "a"}}],
            verifies=[{"op": "assert", "sel": {"label": "b"}}],
            verifies_why="b only exists on the settings screen",
        )
        for key in (
            "app", "name", "description", "aliases", "requires", "satisfiedWhen", "steps",
            "verifies", "verifiesWhy", "keyMap", "provenance", "history", "timing", "health",
        ):
            self.assertIn(key, record)
        self.assertEqual(record["app"], "com.example.Demo")
        self.assertEqual(record["health"], {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None})
        self.assertTrue(record["provenance"].get("verified"))


class RecordCommandTests(unittest.TestCase):
    def setUp(self):
        from uiflow.commands import record as record_command

        self.command = record_command
        self.dir = tempfile.mkdtemp(prefix="uiflow-records-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = self.command.records.paths.RECORDS_DIR
        self.command.records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, self.command.records.paths, "RECORDS_DIR", saved)
        self.command.records.invalidate()

    def build(self, steps, **kwargs):
        kwargs.setdefault("warn", lambda line: None)
        return self.command.build_record(
            app="com.example.Demo",
            name="thing",
            steps=steps,
            verifies_why=kwargs.pop("verifies_why", "because"),
            **kwargs
        )

    def test_refuses_empty_steps(self):
        with self.assertRaises(self.command.RecordRefused) as caught:
            self.build([])
        self.assertIn("no steps", str(caught.exception))

    def test_refuses_a_missing_verifies_why(self):
        with self.assertRaises(self.command.RecordRefused):
            self.build([{"op": "tap"}], verifies_why="")

    def test_refuses_literal_typed_text_without_keep_text(self):
        with self.assertRaises(self.command.RecordRefused) as caught:
            self.build([{"op": "type", "text": "s3cret"}])
        self.assertIn("--keep-text", str(caught.exception))

    def test_keep_text_allows_it(self):
        record = self.build([{"op": "type", "text": "s3cret"}], keep_text=True)
        self.assertEqual(record["steps"][0]["text"], "s3cret")

    def test_warns_loudly_when_satisfied_when_is_empty(self):
        warnings = []
        record = self.build([{"op": "tap"}], warn=warnings.append)
        self.assertTrue(any("satisfiedWhen" in warning for warning in warnings))
        self.assertEqual(record["satisfiedWhen"], [])


class KindTests(unittest.TestCase):
    def test_to_record_omits_kind_for_a_flow(self):
        built = journal.to_record("a", "n", [{"op": "tap"}])
        self.assertNotIn("kind", built)

    def test_to_record_writes_kind_for_a_fragment(self):
        built = journal.to_record("a", "n", [{"op": "tap"}], kind="fragment")
        self.assertEqual(built["kind"], "fragment")


class ProvenanceTests(unittest.TestCase):
    def test_a_record_without_provenance_has_no_recordedOn_key(self):
        self.assertNotIn("recordedOn", journal.to_record("com.example.Demo", "f", [{"op": "ping"}]))

    def test_provenance_is_stored_when_supplied(self):
        record = journal.to_record(
            "com.example.Demo", "f", [{"op": "ping"}],
            recorded_on={"device": "iPhone 17 Pro", "w": 402.0, "h": 874.0, "fingerprint": "402x874"},
        )
        self.assertEqual(record["recordedOn"]["fingerprint"], "402x874")
        self.assertEqual(record["recordedOn"]["device"], "iPhone 17 Pro")

    def test_an_empty_provenance_is_treated_as_absent(self):
        self.assertNotIn(
            "recordedOn", journal.to_record("com.example.Demo", "f", [{"op": "ping"}], recorded_on={})
        )

    def test_only_the_four_known_fields_are_kept(self):
        record = journal.to_record(
            "com.example.Demo", "f", [{"op": "ping"}],
            recorded_on={"device": "iPad", "w": 1024.0, "h": 1366.0, "fingerprint": "1024x1366",
                         "source": "idb", "density": 2.0},
        )
        self.assertEqual(sorted(record["recordedOn"]), ["device", "fingerprint", "h", "w"])

    def test_a_null_device_name_does_not_create_the_key(self):
        record = journal.to_record(
            "com.example.Demo", "f", [{"op": "ping"}],
            recorded_on={"device": None, "w": 402.0, "h": 874.0, "fingerprint": "402x874"},
        )
        self.assertNotIn("device", record["recordedOn"])
        self.assertEqual(record["recordedOn"]["fingerprint"], "402x874")


class ScaffoldingTests(unittest.TestCase):
    def test_a_screen_probe_is_never_baked_into_a_record(self):
        self.assertEqual(journal.to_steps([{"op": "screen", "ok": True}, {"op": "tap", "ok": True}]),
                         [{"op": "tap"}])


if __name__ == "__main__":
    unittest.main()
