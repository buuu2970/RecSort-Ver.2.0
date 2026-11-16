"""Windows にログインしたときに RecSort（サーバーだけ）を起動する設定。

- 利用者が設定画面（またはコマンド）で明示的に有効にしたときだけ登録する。既定では登録しない
- 登録先は、そのユーザーだけの「HKEY_CURRENT_USER\\...\\Run」（管理者権限は不要。ほかのユーザーには影響しない）
- 起動するのは黒い窓を出さない pythonw.exe で、ブラウザは開かない（main.py --background）
- 解除は同じ設定画面・コマンドから行う。登録した値（RecSort）だけを消し、ほかのアプリの登録には触れない
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RecSort"
ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


class Unsupported(Exception):
    """この OS では自動起動を設定できない。"""


class WinRegistry:
    """HKEY_CURRENT_USER の Run キーだけを読み書きする。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise Unsupported("ログイン時の自動起動は Windows でだけ設定できます")
        import winreg
        self.winreg = winreg

    def get(self) -> str | None:
        w = self.winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_READ) as k:
                value, _ = w.QueryValueEx(k, VALUE_NAME)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, command: str) -> None:
        w = self.winreg
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as k:
            w.SetValueEx(k, VALUE_NAME, 0, w.REG_SZ, command)

    def delete(self) -> None:
        w = self.winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as k:
                w.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            pass


def background_python(executable: str | None = None) -> str:
    """黒い窓を出さない pythonw.exe（同じ Python・同じ .venv のもの）。なければ今の Python。"""
    exe = Path(executable or sys.executable)
    candidate = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    return str(candidate if candidate.exists() else exe)


def command(executable: str | None = None, port: int | None = None) -> str:
    """登録するコマンド。パスに日本語や空白があっても動くよう、引用符で囲む。"""
    cmd = f'"{background_python(executable)}" "{MAIN}" --background'
    if port:
        cmd += f" --port {int(port)}"
    return cmd


def status(registry=None, port: int | None = None) -> dict:
    try:
        reg = registry or WinRegistry()
    except Unsupported as e:
        return {"supported": False, "enabled": False, "command": None, "current": False, "message": str(e)}
    current = reg.get()
    expected = command(port=port)
    result = {"supported": True, "enabled": current is not None, "command": current, "current": current == expected,
              "message": ""}
    if current is not None and current != expected:
        result["message"] = ("登録されている起動コマンドが、今の RecSort の場所と違います（フォルダを移動した可能性があります）。"
                             "いったん解除して、もう一度有効にしてください")
    return result


def enable(registry=None, port: int | None = None) -> dict:
    reg = registry or WinRegistry()
    reg.set(command(port=port))
    return status(reg, port)


def disable(registry=None, port: int | None = None) -> dict:
    reg = registry or WinRegistry()
    reg.delete()
    return status(reg, port)
