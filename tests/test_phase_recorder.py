import json
import threading
import time
import unittest

from wxbg.phase_recorder import FLAG_NAMES, PHASES, PhaseRecorder


def flag_sample(**overrides):
    sample = {name: False for name in FLAG_NAMES}
    sample.update(overrides)
    return sample


def assert_coherent(test_case, snapshot):
    phase_counts = snapshot["phase_sample_counts"]
    test_case.assertEqual(sum(phase_counts.values()), snapshot["sample_count"])
    test_case.assertEqual(set(phase_counts), set(PHASES))
    for metadata in snapshot["flags"].values():
        first = metadata["first_sample_index"]
        if first is not None:
            test_case.assertGreaterEqual(first, 0)
            test_case.assertLess(first, snapshot["sample_count"])
            test_case.assertIn(metadata["phase_at_first_hit"], PHASES)
        test_case.assertLessEqual(metadata["hit_count"], snapshot["sample_count"])


class PhaseRecorderTests(unittest.TestCase):
    def test_image_operation_phase_names_are_fixed_and_recorded(self):
        recorder = PhaseRecorder()
        recorder.set_phase("image_native_select")
        recorder.observe({name: name == "foreground_changed" for name in FLAG_NAMES})
        sample = recorder.snapshot()
        self.assertEqual(sample["flags"]["foreground_changed"]["phase_at_first_hit"],
                         "image_native_select")
        self.assertEqual(sample["phase_sample_counts"]["image_native_select"], 1)

    def test_empty_snapshot_is_fixed_json_metadata(self):
        recorder = PhaseRecorder()
        snapshot = recorder.snapshot()

        self.assertEqual(recorder.summary(), snapshot)
        self.assertEqual(snapshot["schema"], "phase_recorder_v1")
        self.assertEqual(snapshot["phase"], "operation")
        self.assertEqual(snapshot["sample_count"], 0)
        self.assertFalse(snapshot["overflow"])
        self.assertEqual(snapshot["phase_sample_counts"], {phase: 0 for phase in PHASES})
        self.assertEqual(set(snapshot["flags"]), set(FLAG_NAMES))
        for metadata in snapshot["flags"].values():
            self.assertEqual(metadata, {
                "first_sample_index": None,
                "hit_count": 0,
                "phase_at_first_hit": None,
            })
        json.dumps(snapshot)

    def test_phase_is_a_closed_enum(self):
        recorder = PhaseRecorder()
        recorder.set_phase("enter_contacts")
        self.assertEqual(recorder.snapshot()["phase"], "enter_contacts")

        for invalid in ("ENTER_CONTACTS", "", None, 1):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "^invalid_phase$"):
                recorder.set_phase(invalid)

    def test_observe_requires_exact_boolean_flag_shape(self):
        recorder = PhaseRecorder()
        valid = flag_sample()
        invalid_samples = [
            {name: value for name, value in valid.items() if name != FLAG_NAMES[0]},
            dict(valid, unexpected=True),
            dict(valid, foreground_changed=1),
            dict(valid, clipboard_changed="false"),
            list(valid.values()),
            "foreground_changed",
        ]

        for invalid in invalid_samples:
            with self.subTest(invalid=repr(invalid)), self.assertRaisesRegex(
                ValueError, "^invalid_phase_recorder_flags$"
            ):
                recorder.observe(invalid)

        self.assertEqual(recorder.snapshot()["sample_count"], 0)

    def test_first_hit_records_observation_phase_and_later_hits_only_increment(self):
        recorder = PhaseRecorder()
        recorder.set_phase("enter_contacts")
        recorder.observe(flag_sample(foreground_changed=True, capture_observed=True))
        recorder.set_phase("restore_chat")
        recorder.observe(flag_sample(monitor_error=True, capture_observed=True))

        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["sample_count"], 2)
        self.assertEqual(snapshot["phase_sample_counts"]["enter_contacts"], 1)
        self.assertEqual(snapshot["phase_sample_counts"]["restore_chat"], 1)
        self.assertEqual(snapshot["flags"]["foreground_changed"], {
            "first_sample_index": 0,
            "hit_count": 1,
            "phase_at_first_hit": "enter_contacts",
        })
        self.assertEqual(snapshot["flags"]["capture_observed"], {
            "first_sample_index": 0,
            "hit_count": 2,
            "phase_at_first_hit": "enter_contacts",
        })
        self.assertEqual(snapshot["flags"]["monitor_error"], {
            "first_sample_index": 1,
            "hit_count": 1,
            "phase_at_first_hit": "restore_chat",
        })
        self.assertEqual(snapshot["flags"]["cursor_changed"]["first_sample_index"], None)

    def test_concurrent_observe_and_snapshot_keep_counts_coherent(self):
        recorder = PhaseRecorder(max_samples=401)
        worker_count = 4
        observations_per_worker = 100
        ready = threading.Barrier(worker_count + 1)
        finished = threading.Event()
        errors = []

        def worker(worker_index):
            try:
                recorder.set_phase(PHASES[worker_index])
                ready.wait()
                for index in range(observations_per_worker):
                    recorder.observe(flag_sample(
                        target_restored=(index % 2 == 0),
                        capture_observed=(worker_index == 0),
                    ))
            except BaseException as error:  # report thread failures in the test thread
                errors.append(error)

        def sampler():
            try:
                ready.wait()
                while not finished.is_set():
                    assert_coherent(self, recorder.snapshot())
                    time.sleep(0)
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
        observer = threading.Thread(target=sampler)
        for thread in workers:
            thread.start()
        observer.start()
        for thread in workers:
            thread.join()
        finished.set()
        observer.join()

        self.assertEqual(errors, [])
        final = recorder.snapshot()
        assert_coherent(self, final)
        self.assertEqual(final["sample_count"], worker_count * observations_per_worker)
        self.assertFalse(final["overflow"])

    def test_capacity_saturates_and_marks_explicit_overflow(self):
        recorder = PhaseRecorder(max_samples=2)
        recorder.observe(flag_sample(foreground_changed=True))
        recorder.observe(flag_sample(clipboard_changed=True))
        before_overflow = recorder.snapshot()

        recorder.observe(flag_sample(cursor_changed=True, monitor_error=True))
        after_overflow = recorder.snapshot()

        self.assertEqual(after_overflow["sample_count"], 2)
        self.assertEqual(after_overflow["phase_sample_counts"]["operation"], 2)
        self.assertTrue(after_overflow["overflow"])
        self.assertEqual(after_overflow["flags"]["cursor_changed"]["hit_count"], 0)
        self.assertEqual(after_overflow["flags"]["monitor_error"]["hit_count"], 0)
        self.assertEqual(after_overflow["flags"]["foreground_changed"],
                         before_overflow["flags"]["foreground_changed"])

    def test_metadata_space_stays_fixed_after_many_samples(self):
        recorder = PhaseRecorder(max_samples=5000)
        for _ in range(1000):
            recorder.observe(flag_sample())
        recorder.observe(flag_sample(new_visible_window=True))

        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["sample_count"], 1001)
        self.assertEqual(len(snapshot["flags"]), len(FLAG_NAMES))
        self.assertNotIn("samples", snapshot)
        self.assertNotIn("events", snapshot)
        self.assertEqual(snapshot["flags"]["new_visible_window"]["hit_count"], 1)
        self.assertEqual(snapshot["flags"]["new_visible_window"]["first_sample_index"], 1000)
        json.dumps(snapshot)

    def test_capacity_must_be_positive_integer(self):
        for invalid in (0, -1, True, 1.0):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "^invalid_phase_recorder_capacity$"
            ):
                PhaseRecorder(max_samples=invalid)


if __name__ == "__main__":
    unittest.main()
