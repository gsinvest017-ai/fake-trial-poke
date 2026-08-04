"""跨平台可載入性的離線測試。

存在的理由：scheduler.py 曾在模組層 `import msvcrt`，於是任何非 Windows
平台連 `import scheduler` 都會 ImportError——在 Windows 上跑全套測試也永遠
看不到。這裡用 AST 靜態檢查取代「換台機器才發現」，因此在任何平台都成立。
"""

from __future__ import annotations

import ast
import pathlib
import re
import shlex
import subprocess
import sys
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import scheduler
import service

SCHEDULE_SCRIPT = PACKAGE_DIR / "schedule_morning.sh"

#: install.sh、schedule_morning.sh 與這裡共用的同一條判準。
VERSION_PROBE = (
    "import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,12) "
    'and struct.calcsize("P")*8==64 else 1)'
)

#: 只存在於單一平台的標準函式庫模組。硬 import 任何一個，另一邊就整個載不動。
PLATFORM_ONLY_MODULES = {
    "msvcrt": "Windows",
    "winreg": "Windows",
    "winsound": "Windows",
    "fcntl": "POSIX",
    "termios": "POSIX",
    "pwd": "POSIX",
    "grp": "POSIX",
}


def _guarded_import_names(tree: ast.AST) -> set[str]:
    """被 try/except 包住的 import 名稱——那才是可接受的寫法。"""
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                guarded.update(alias.name.split(".")[0] for alias in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.module:
                guarded.add(inner.module.split(".")[0])
    return guarded


def _module_level_imports(tree: ast.Module) -> set[str]:
    """只看模組層；函式內的延遲 import 不會影響模組能不能被載入。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class PlatformImportTests(unittest.TestCase):
    def test_no_unguarded_platform_only_imports(self) -> None:
        sources = sorted(PACKAGE_DIR.glob("*.py"))
        self.assertTrue(sources, "沒有掃到任何模組，測試本身失效了")
        for path in sources:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                guarded = _guarded_import_names(tree)
                for name in _module_level_imports(tree) & PLATFORM_ONLY_MODULES.keys():
                    self.assertIn(
                        name,
                        guarded,
                        f"{path.name} 在模組層硬 import 了只有 "
                        f"{PLATFORM_ONLY_MODULES[name]} 才有的 {name}；"
                        "請包進 try/except ImportError 並提供另一平台的備援。",
                    )

    def test_lock_helpers_have_a_backend_on_this_platform(self) -> None:
        """兩個模組都必須在本平台找得到可用的鎖，不能安靜地不鎖。"""
        for module in (scheduler, service):
            with self.subTest(module=module.__name__):
                self.assertTrue(
                    module.msvcrt is not None or module.fcntl is not None,
                    f"{module.__name__} 在本平台沒有任何檔案鎖後端",
                )


class SchedulerInstanceLockTests(unittest.TestCase):
    def test_second_acquire_is_refused_and_release_frees_it(self) -> None:
        handle = scheduler.acquire_instance_lock()
        try:
            with self.assertRaises(RuntimeError):
                scheduler.acquire_instance_lock()
        finally:
            scheduler.release_instance_lock(handle)
        # 釋放後必須能重新取得，否則看門狗重啟一次就再也起不來。
        scheduler.release_instance_lock(scheduler.acquire_instance_lock())


@unittest.skipUnless(sys.platform == "darwin", "schedule_morning.sh 只在 macOS 上有載體")
class ScheduleMorningPythonResolutionTests(unittest.TestCase):
    """排程挑錯直譯器時，失敗是靜默的——08:25 沒有人在看畫面。

    2026-08-04 於 macOS 實機重現：舊版 resolve_python 最後會退回
    `command -v python3`，而 launchd 給的 PATH 是 /usr/bin:/bin:/usr/sbin:/sbin，
    裡面既沒有 Homebrew 也沒有 pyenv，於是那句話拿到的是系統內建的
    /usr/bin/python3（3.9）。service.py 接著死在 `import webserver` 的
    `Callable[...] | Any` TypeError 上，只留一行在 log/schedule.log 裡，
    每日錄製就這樣安靜地永遠不發生。

    這裡直接把腳本裡的兩個解析函式挖出來執行，測的是真的那份程式碼。
    """

    #: 兩個函式都是頂層定義、以 `^}` 收尾，所以行範圍抽取是穩的。
    HELPERS = "/^python_is_supported()/,/^}/p; /^resolve_python()/,/^}/p"

    def _sourced(self, body: str) -> subprocess.CompletedProcess[str]:
        script = (
            f"eval \"$(sed -n {shlex.quote(self.HELPERS)} "
            f"{shlex.quote(str(SCHEDULE_SCRIPT))})\"\n{body}"
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_resolved_interpreter_can_actually_run_this_project(self) -> None:
        """核心不變式：交出來的直譯器一定通得過版本閘門，否則寧可不交。"""
        proc = self._sourced("PROJECT_DIR=/nonexistent\nresolve_python")
        if proc.returncode != 0:
            self.skipTest("這台機器上沒有任何 Python 3.12，無從驗證")

        resolved = proc.stdout.strip()
        self.assertTrue(resolved, "回報成功卻沒有印出路徑")
        probe = subprocess.run([resolved, "-c", VERSION_PROBE], timeout=60)
        self.assertEqual(
            probe.returncode,
            0,
            f"resolve_python 交出了跑不動本專案的 {resolved}；"
            "排程會在 log/schedule.log 裡以 TypeError 靜默失敗。",
        )

    def test_version_gate_agrees_with_the_real_interpreter(self) -> None:
        """閘門本身不能說謊——它是上面那條不變式唯一的依據。"""
        for candidate in ("/usr/bin/python3", sys.executable):
            path = pathlib.Path(candidate)
            if not path.exists():
                continue
            with self.subTest(interpreter=candidate):
                truth = subprocess.run(
                    [candidate, "-c", VERSION_PROBE], timeout=60
                ).returncode == 0
                gate = self._sourced(
                    f"python_is_supported {shlex.quote(candidate)}"
                ).returncode == 0
                self.assertEqual(
                    gate,
                    truth,
                    f"python_is_supported 對 {candidate} 的判定與實際版本不符",
                )

    def test_no_unversioned_python3_fallback(self) -> None:
        """靜態擋回歸：任何一條「隨便找個 python3」的退路都不可以再出現。"""
        # 不能只看整行有沒有出現 "python3.12"——原本那條 bug 就寫成
        # `command -v python3.12 || command -v python3 || true`，同一行裡
        # 兩者都在。要找的是「python3 後面沒有接版本號」的那一個。
        source = SCHEDULE_SCRIPT.read_text(encoding="utf-8")
        bare_python3 = re.compile(r"command -v python3(?!\.\d)")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if bare_python3.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "schedule_morning.sh 又出現了不指定版本的 python3 退路："
            f"{offenders}；在 macOS 上那會拿到系統內建的 3.9。",
        )


if __name__ == "__main__":
    unittest.main()
