"""Harbor validates NovaCode trial knobs before running a paid trial."""

import unittest

from deepagents_harbor.novacode_wrapper import NovaCodeWrapper


class NovaCodeOptionsTest(unittest.TestCase):
    def test_known_trial_knobs_are_accepted(self):
        NovaCodeWrapper.preflight({"reasoning_effort": "off", "learning": "frozen"})
        self.assertIn("reasoning_effort", NovaCodeWrapper.options_schema()["properties"])

    def test_typo_is_rejected_before_trial(self):
        with self.assertRaisesRegex(ValueError, "Unknown option 'reasoning_efort'"):
            NovaCodeWrapper.preflight({"reasoning_efort": "off"})


if __name__ == "__main__":
    unittest.main()
