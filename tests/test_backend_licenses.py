"""后台许可收集回归：只收集实际产物依赖，保留原文并拒绝未知许可缺口。"""

import importlib.util
from pathlib import Path
import tempfile
import unittest


_script = Path(__file__).resolve().parent.parent / "scripts/collect_backend_licenses.py"
_spec = importlib.util.spec_from_file_location("backend_license_collector", _script)
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


class FakeDistribution:
    def __init__(self, root, name, files):
        self.root = root
        self.metadata = {"Name": name, "License-Expression": "MIT"}
        self.version = "1.0.0"
        self.files = [Path(value) for value in files]

    def locate_file(self, path):
        return self.root / path


class BackendLicenseTests(unittest.TestCase):
    def test_only_actual_distribution_notices_are_copied_without_metadata_or_user_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed"
            installed.mkdir()
            originals = {"used.py": b"# code", "LICENSE.txt": b"copyright synthetic author\r\nMIT text\r\n",
                         "unused.py": b"# unrelated", "NOTICE.md": b"unused notice",
                         "credentials.json": b"private-test-value", "license_helpers.py": b"# not a notice"}
            for name, content in originals.items():
                (installed / name).write_bytes(content)
            active = FakeDistribution(installed, "used", ["used.py", "LICENSE.txt", "credentials.json", "license_helpers.py"])
            unrelated = FakeDistribution(installed, "unused", ["unused.py", "NOTICE.md"])
            output = root / "output"
            result = collector.collect_python_distributions({(installed / "used.py").resolve()}, output, [active, unrelated])
            self.assertEqual([entry["name"] for entry in result], ["used"])
            files = [p for p in output.rglob("*") if p.is_file()]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), originals["LICENSE.txt"])
            self.assertFalse(any("credentials" in str(path) for path in files))
            self.assertNotIn(str(installed), str(result))

    def test_missing_license_text_and_escaping_record_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "included.py"
            module.write_text("# synthetic module")
            for files in (["included.py"], ["included.py", "../LICENSE.txt"]):
                with self.subTest(files=files), self.assertRaises(ValueError):
                    collector.collect_python_distributions({module.resolve()}, root / "output",
                                                          [FakeDistribution(root, "dependency", files)])

    def test_toc_is_parsed_as_data_without_executing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toc = root / "Analysis.toc"
            source = root / "module.py"
            toc.write_text(repr([("module", str(source), "PYMODULE"), ("ignored", "not-an-input", "OTHER")]))
            self.assertEqual(collector.included_sources(toc), {source.resolve()})
            marker = root / "must-not-exist"
            toc.write_text(f"__import__('pathlib').Path({str(marker)!r}).touch()")
            with self.assertRaises((ValueError, SyntaxError)):
                collector.included_sources(toc)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
