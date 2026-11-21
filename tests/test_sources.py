"""Regression scan of production Python sources."""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = (
    "/home/random",
    "Kels_KNF",
    "Tartube_videos",
    "node.exe",
    "--js-runtimes",
)


def production_sources():
    paths = [os.path.join(ROOT, "livegrab.py")]
    library = os.path.join(ROOT, "livegrab_lib")
    for dirpath, dirnames, filenames in os.walk(library):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                paths.append(os.path.join(dirpath, name))
    return paths


class ProductionSourceTests(unittest.TestCase):
    def test_sources_omit_machine_paths_and_javascript_runtimes(self):
        hits = []
        for path in production_sources():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for token in BANNED:
                if token in text:
                    hits.append(f"{path} contains {token!r}")
        self.assertEqual(hits, [], "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
