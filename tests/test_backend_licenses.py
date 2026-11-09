"""后台许可收集回归：只收集实际产物依赖，保留原文并拒绝未知许可缺口。"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


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
    def _prepare_backend(self, root):
        """建立隔离的虚构打包产物；测试不读取本机 SDK、运行库或真实发行目录。"""
        backend = root / "dist/backend/synthv-backend"
        backend.mkdir(parents=True)
        (backend / "synthv-backend.exe").write_bytes(b"synthetic executable")
        analysis = root / "build/desktop-backend/synthv-backend/Analysis-00.toc"
        analysis.parent.mkdir(parents=True)
        analysis.write_text("[]", encoding="utf-8")
        python = root / "python-fixture"
        python.mkdir()
        (python / "LICENSE.txt").write_bytes(b"synthetic Python license\r\n")
        return backend, python

    def test_new_package_without_capture_does_not_require_or_declare_dotnet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend, python = self._prepare_backend(root)
            with (mock.patch.object(collector.sys, "base_prefix", str(python)),
                  mock.patch.object(collector.metadata, "distributions", return_value=[]),
                  mock.patch.object(collector, "dotnet_notice_sources", side_effect=AssertionError("不应读取 .NET"))):
                result = collector.collect(root)
            manifest = json.loads((backend / "licenses/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result, {"pythonDistributions": 0, "licenseFiles": 1})
            self.assertNotIn("dotnetRuntimeVersion", manifest)
            self.assertNotIn("dotnetRuntime", manifest["sources"])
            self.assertFalse((backend / "licenses/dotnet-runtime").exists())
            self.assertNotIn(".NET", (backend / "licenses/README.txt").read_text(encoding="utf-8"))
            self.assertEqual((backend / "licenses/python-runtime/LICENSE.txt").read_bytes(),
                             b"synthetic Python license\r\n")

    def test_legacy_capture_collects_only_its_actual_runtime_notices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend, python = self._prepare_backend(root)
            capture = backend / "_internal/native/ProcessAudioCapture"
            capture.mkdir(parents=True)
            (capture / "ProcessAudioCapture.runtimeconfig.json").write_text(json.dumps({
                "runtimeOptions": {"includedFrameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.0"}]}
            }), encoding="utf-8")
            pack = root / "nuget-fixture/microsoft.netcore.app.runtime.win-x64/10.0.0"
            pack.mkdir(parents=True)
            for name in ("LICENSE.TXT", "THIRD-PARTY-NOTICES.TXT"):
                (pack / name).write_text("synthetic upstream notice\n", encoding="utf-8")
            assets = root / "native/ProcessAudioCapture/obj/project.assets.json"
            assets.parent.mkdir(parents=True)
            assets.write_text(json.dumps({"packageFolders": {str(root / "nuget-fixture"): {}}}), encoding="utf-8")
            with (mock.patch.object(collector.sys, "base_prefix", str(python)),
                  mock.patch.object(collector.metadata, "distributions", return_value=[])):
                result = collector.collect(root)
            manifest = json.loads((backend / "licenses/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["licenseFiles"], 3)
            self.assertEqual(manifest["dotnetRuntimeVersion"], "10.0.0")
            self.assertEqual((backend / "licenses/dotnet-runtime/LICENSE.TXT").read_bytes(),
                             (pack / "LICENSE.TXT").read_bytes())
            self.assertNotIn(str(root), json.dumps(manifest))

    def test_stale_dotnet_license_directory_requires_clean_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend, _ = self._prepare_backend(root)
            stale = backend / "licenses/dotnet-runtime/LICENSE.TXT"
            stale.parent.mkdir(parents=True)
            stale.write_text("old notice", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重新构建"):
                collector.collect(root)
            self.assertEqual(stale.read_text(encoding="utf-8"), "old notice")

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
