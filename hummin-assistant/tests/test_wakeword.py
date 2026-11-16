import unittest

from hummin_assistant.wakeword import Detector


class DetectorTests(unittest.TestCase):
    def test_fires_once_per_refractory_window(self):
        d = Detector(0.5, 3)
        scores = [0.1, 0.6, 0.7, 0.9, 0.2, 0.8]
        self.assertEqual([d.update(s) for s in scores], [False, True, False, False, False, True])
