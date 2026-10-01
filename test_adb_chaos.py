import json
import tempfile
import unittest
from pathlib import Path

import adb_chaos as ac


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example.app" content-desc="" clickable="false"
        scrollable="false" enabled="true" visible-to-user="true"
        bounds="[0,0][1080,1920]">
    <node index="1" text="Open" resource-id="com.example.app:id/open"
          class="android.widget.Button" package="com.example.app"
          content-desc="" clickable="true" scrollable="false"
          enabled="true" visible-to-user="true"
          bounds="[100,300][500,420]" />
    <node index="2" text="Delete everything"
          resource-id="com.example.app:id/delete"
          class="android.widget.Button" package="com.example.app"
          content-desc="" clickable="true" scrollable="false"
          enabled="true" visible-to-user="true"
          bounds="[100,500][500,620]" />
    <node index="3" text="" resource-id=""
          class="android.widget.ScrollView" package="com.example.app"
          content-desc="" clickable="false" scrollable="true"
          enabled="true" visible-to-user="true"
          bounds="[0,500][1080,1700]" />
  </node>
</hierarchy>
"""


class TestPureLogic(unittest.TestCase):
    def test_required_runtime_names_exist(self):
        required = (
            "smart_tap",
            "smart_swipe",
            "smart_decision",
            "run_chaos",
            "save_session_metadata",
            "get_current_window",
            "make_ui_snapshot",
        )
        for name in required:
            self.assertTrue(
                callable(getattr(ac, name, None)),
                name,
            )

    def test_normalize_dynamic_values(self):
        value = ac.normalize_text("Battery 91% at 21:37")
        self.assertIn("<time>", value)
        self.assertIn("<n>", value)

    def test_parse_devices(self):
        raw = (
            "List of devices attached\n"
            "ABC123 device product:foo model:Pixel_8\n"
            "XYZ999 unauthorized usb:1-1\n"
        )
        devices = ac.parse_devices(raw)
        self.assertEqual(len(devices), 2)
        self.assertTrue(devices[0].authorized)
        self.assertEqual(devices[0].model, "Pixel 8")

    def test_parse_ui_filters_blocked_target(self):
        nodes = ac.parse_ui_xml(SAMPLE_XML)
        self.assertEqual(len(nodes), 4)

        snapshot = ac.UiSnapshot(
            xml=SAMPLE_XML,
            nodes=nodes,
            package="com.example.app",
            activity=".MainActivity",
            fingerprint="test-state",
        )

        labels = [node.label for node in snapshot.clickable]
        self.assertIn("Open", labels)
        self.assertNotIn("Delete everything", labels)
        self.assertEqual(len(snapshot.scrollables), 1)

    def test_bounds_center_and_clamp(self):
        bounds = ac.Bounds(10, 20, 110, 220)
        self.assertEqual(bounds.center, (60, 120))
        self.assertEqual(
            bounds.clamp(80, 100),
            ac.Bounds(10, 20, 80, 100),
        )

    def test_brain_learns_transition(self):
        brain = ac.Brain()
        brain.record_result(
            "A",
            "open",
            "B",
            True,
            True,
        )
        stats = brain.target("A", "open")
        self.assertEqual(stats.attempts, 1)
        self.assertEqual(stats.changed, 1)
        self.assertEqual(stats.novel, 1)
        self.assertEqual(brain.graph["A"]["open"], "B")

    def test_brain_round_trip(self):
        brain = ac.Brain()
        brain.record_result(
            "A",
            "open",
            "B",
            True,
            True,
        )
        brain.states["A"] = ac.StateStats(
            package="com.example",
            activity=".Main",
            visits=2,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            brain.save(path)
            restored = ac.Brain.load(path)

        self.assertIn("A", restored.states)
        self.assertIn("A::open", restored.targets)
        self.assertEqual(
            restored.graph["A"]["open"],
            "B",
        )

    def test_bad_memory_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(
                '{"states":{"A":{"visits":"not-a-number"}}}',
                encoding="utf-8",
            )
            brain = ac.Brain.load(path)

        self.assertIsInstance(brain, ac.Brain)
        self.assertEqual(
            brain.states["A"].visits,
            0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
