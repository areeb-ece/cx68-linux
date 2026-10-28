import tempfile
import unittest
from pathlib import Path

from cx68bridge import access as A


class AccessTest(unittest.TestCase):
    def test_program_name_unix_and_wine(self):
        self.assertEqual(A.program_name(b"/usr/bin/firefox\0--new-tab\0"), "firefox")
        self.assertEqual(A.program_name(b"Z:\\home\\u\\steamapps\\common\\assettocorsa\\acs.exe\0"), "acs.exe")
        self.assertEqual(A.program_name(b"C:\\Program Files\\Content Manager.exe\0"), "Content Manager.exe")
        self.assertEqual(A.program_name(b"", "kworker"), "kworker")

    def test_matches_case_insensitive(self):
        pol = A.Policy("allowlist", ["acs.exe", "Content Manager.exe"])
        running = {"ACS.EXE": [10], "firefox": [11], "steam": [12]}
        self.assertEqual(A.matches(pol, running), ["ACS.EXE"])
        self.assertEqual(A.matches(pol, {"firefox": [1]}), [])

    def test_save_load_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "access.json"
            self.assertEqual(A.load(path), A.Policy())                  # missing file: allowlist defaults
            A.save(A.Policy("always", ["x.exe"]), path)
            self.assertEqual(A.load(path), A.Policy("always", ["x.exe"]))
            path.write_text('{"mode": "bogus", "programs": ["a", " "]}')
            self.assertEqual(A.load(path), A.Policy("allowlist", ["a"]))

    def test_running_programs_sees_this_process(self):
        self.assertTrue(any(n.startswith("python") for n in A.running_programs()))


if __name__ == "__main__":
    unittest.main()
