"""跨平台可載入性的離線測試。

存在的理由：scheduler.py 曾在模組層 `import msvcrt`，於是任何非 Windows
平台連 `import scheduler` 都會 ImportError——在 Windows 上跑全套測試也永遠
看不到。這裡用 AST 靜態檢查取代「換台機器才發現」，因此在任何平台都成立。
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import scheduler
import service

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


if __name__ == "__main__":
    unittest.main()
