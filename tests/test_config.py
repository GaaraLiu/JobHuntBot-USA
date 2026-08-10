from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_default_weights_are_complete_and_thresholds_ordered(self) -> None:
        config = load_config()
        self.assertEqual(sum(config.scoring_weights.values()), 100)
        self.assertGreater(config.thresholds.apply_min, config.thresholds.review_min)

    def test_partial_override_is_deep_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "config.json"
            override.write_text(json.dumps({"thresholds": {"apply_min": 80}}), encoding="utf-8")
            config = load_config(override)
        self.assertEqual(config.thresholds.apply_min, 80)
        self.assertEqual(config.thresholds.review_min, 50)

    def test_invalid_weights_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "config.json"
            override.write_text(json.dumps({"scoring_weights": {"role_title": 1}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(override)


if __name__ == "__main__":
    unittest.main()
