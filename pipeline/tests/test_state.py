import unittest

from pipeline.optcycle.state import (
    ExperimentState,
    StateError,
    can_keep,
    require_state,
)


class StateTests(unittest.TestCase):
    def test_arena_rejects_candidate_ready_state(self):
        require_state(
            ExperimentState.READY_FOR_ARENA,
            {ExperimentState.READY_FOR_ARENA},
            "arena",
        )

        with self.assertRaisesRegex(StateError, "arena requires READY_FOR_ARENA"):
            require_state(
                ExperimentState.CANDIDATE_READY,
                {ExperimentState.READY_FOR_ARENA},
                "arena",
            )

    def test_keep_requires_complete_passing_attempt(self):
        complete = {
            "arena": {
                "attempts": [
                    {
                        "accuracy_passed": True,
                        "kernels": {
                            "sliding_project_qkv": {"cycles": 250288},
                            "sliding_attention_output": {"cycles": 409907},
                            "decoder_feedforward": {"cycles": 3704175},
                        },
                    }
                ]
            }
        }
        missing_cycle = {
            "arena": {
                "attempts": [
                    {
                        "accuracy_passed": True,
                        "kernels": {
                            "sliding_project_qkv": {"cycles": 250288},
                            "sliding_attention_output": {"cycles": 409907},
                        },
                    }
                ]
            }
        }

        self.assertTrue(can_keep(complete))
        self.assertFalse(can_keep(missing_cycle))
        self.assertFalse(
            can_keep({"arena": {"attempts": [{"accuracy_passed": False}]}})
        )


if __name__ == "__main__":
    unittest.main()
