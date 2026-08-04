# -*- mode: python ; coding: utf-8 -*-
"""macOS .app 打包設定。

不是 gs-app-pack 產生的——gs-app-pack 是 PowerShell + Inno Setup 的
Windows 產線。這份是手寫並納入版控的（.gitignore 對它開了例外），
Windows 的 fake-trial-poke.spec 仍由 gs-app-pack 自動生成。

建置：
    ./系統檔案/.venv/bin/python -m PyInstaller fake-trial-poke-macos.spec --clean

與 Windows spec 的三個實質差異：
  * 路徑分隔符用 '/'。Windows spec 裡的 '系統檔案\\ui' 在 macOS 會被當成
    一個含反斜線字元的檔名，找不到就是找不到。
  * upx=False。UPX 會破壞 macOS 的程式碼簽章，簽過的 .app 一壓就跑不動。
  * 多一層 BUNDLE()，產出真正的 .app；沒有它只會得到一個 Finder 不認得、
    也拿不到 GUI session 的裸執行檔。
"""
import os
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH)
SOURCE_DIR = SPEC_DIR / '系統檔案'

# .icns 不在版控內（.gitignore 忽略 static/）。沒有就不指定圖示——
# 用不存在的路徑會讓 PyInstaller 直接失敗，寧可少一個圖示也不要少一個
# 可以出貨的 build。產生方式見 README 的 macOS 章節。
_icon = SPEC_DIR / 'static' / 'gs-icon.icns'
icon = str(_icon) if _icon.is_file() else None

datas = [
    ('系統檔案/ui', 'ui'),
    ('系統檔案/data/holidays.txt', 'data'),
    ('系統檔案/.env.example', '.'),
]
binaries = []
hiddenimports = []

# tkinter 與 webview 跟 Windows 版同理：keyguard 的拒絕視窗是延遲 import
# 的，不明講就可能收不進來，拒絕畫面會安靜降級成純文字——而「看不見的
# 拒絕」跟「應用程式壞掉」在客戶眼中一模一樣。收進來之後可用
# keyguard.appgate.AppGate.refusal_ui() == "window" 驗證。
# macOS 上 webview 還會一併拉進 pyobjc 的 Cocoa / WebKit binding。
#
# bottle / proxy_tools / typing_extensions 是 pywebview 自己宣告的執行期
# 相依，必須逐一點名：collect_all() 只收該套件本身的檔案，不會跟進它的
# 第三方相依；而 app.py 的 `import webview` 寫在 main() 裡（延遲 import），
# 靜態分析看不到模組層的 import，整條相依鏈就這樣斷掉。
#
# 症狀特別會誤導：webview 本體確實會被收進 .app，缺的是 bottle，於是
# app.py 的 except ImportError 會回報「pywebview is missing from this
# build」——一句與事實相反的話。2026-08-04 在 macOS 實機建置時踩到：服務
# 起得來、HTTP 回得了一下，然後行程就退出，視窗從來沒出現過。
for package in (
    'shioaji', 'pysolace', 'tzdata', 'tkinter', 'webview',
    'bottle', 'proxy_tools', 'typing_extensions',
):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
    except Exception as exc:                                  # noqa: BLE001
        # 在這裡明講而不是讓 build 過、到客戶機器上才發現少東西。
        raise SystemExit(
            f"打包中止：收集 {package} 失敗（{exc}）。\n"
            f"請先安裝 macOS 打包相依：\n"
            f"    ./系統檔案/.venv/bin/python -m pip install "
            f"-r 系統檔案/requirements-macos.txt\n"
            f"tkinter 不是 pip 套件；Homebrew Python 需另裝 "
            f"`brew install python-tk@3.12`。"
        ) from exc
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports


a = Analysis(
    ['app.py'],
    pathex=[str(SOURCE_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)


def _openssl_that_ssl_was_built_against():
    """CPython 的 _ssl 實際連結的 libcrypto / libssl 絕對路徑。"""
    import _ssl

    listing = subprocess.run(
        ['otool', '-L', _ssl.__file__],
        capture_output=True, text=True, check=True,
    ).stdout
    wanted = {}
    for line in listing.splitlines()[1:]:
        path = line.strip().split(' (')[0]
        name = os.path.basename(path)
        if name.startswith(('libcrypto.', 'libssl.')):
            wanted[name] = str(Path(path).resolve())
    return wanted


# pysolace 的 wheel 自帶一份 OpenSSL 3.0.8（.dylibs/libcrypto.3.dylib）。
# PyInstaller 依檔名去重，兩份 libcrypto.3.dylib 只會留一份——而它留下的是
# pysolace 那份舊的，同時把 _ssl 的 @rpath 指過去。於是 .app 一 import 任何
# 走 TLS 的東西就死在：
#
#     Symbol not found: _X509_STORE_get1_objects
#
# 那個符號是 OpenSSL 3.2 才加的，3.0.8 沒有。方向錯了才會壞：OpenSSL 3.x
# 系列 ABI 相容，pysolace 對著 3.6.3 跑沒問題，反過來就不行。所以這裡把
# 每一份同名的 libcrypto/libssl 都換成 _ssl 當初編譯時對著的那一份。
#
# 2026-08-04 macOS 實機建置踩到。症狀會誤導：最先爆出來的是
# `could not import pywebview`，因為 webview 進來的路上會碰到 ssl。
_openssl = _openssl_that_ssl_was_built_against()
if _openssl:
    a.binaries = [
        (dest, _openssl.get(os.path.basename(dest), src), kind)
        for dest, src, kind in a.binaries
    ]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='fake-trial-poke',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    # shioaji / pysolace 的 wheel 是單一架構（x86_64 或 arm64），沒有
    # universal2；指定 target_arch 會在合併階段失敗。維持 None＝跟著
    # 目前這台機器的架構走，Intel 與 Apple Silicon 各建一次。
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='fake-trial-poke',
)
app = BUNDLE(
    coll,
    name='Fake Trial Poke.app',
    icon=icon,
    bundle_identifier='com.gsinvest.fake-trial-poke',
    version='0.1.4',
    info_plist={
        'CFBundleName': 'Fake Trial Poke',
        'CFBundleDisplayName': 'Fake Trial Poke',
        'CFBundleShortVersionString': '0.1.4',
        'CFBundleVersion': '0.1.4',
        'LSApplicationCategoryType': 'public.app-category.finance',
        'LSMinimumSystemVersion': '11.0',
        # Retina 下不開這個旗標，pywebview 視窗會被系統放大成模糊點陣。
        'NSHighResolutionCapable': True,
        # 只連 127.0.0.1 的本機 service，其餘不需要例外。
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    },
)
