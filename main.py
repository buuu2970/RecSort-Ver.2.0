"""起動用スクリプト: python main.py（ブラウザで開く）。デスクトップ版は python main.py --desktop

Windows では RecSortを起動.bat をダブルクリックすると、このスクリプトがブラウザ版を起動する。

その他の使い方:
  python main.py --background        サーバーだけを起動（ログイン時の自動起動で使う。ブラウザを開かない）
  python main.py --autostart on      Windows にログインしたときに起動するよう登録する（off で解除、status で確認）
  python main.py --port 8766         別のポート番号で起動する
"""

import sys


def _fail(lines: list[str]) -> int:
    """起動前の問題を伝える。自動起動（黒い窓なし）のときは記録とダイアログでも伝える。"""
    background = "--background" in sys.argv
    if background:
        try:
            from storage_organizer.store import default_data_dir
            from storage_organizer.webapp import notify_failure, open_log
            sys.stdout = sys.stderr = open_log(default_data_dir())
            notify_failure(lines, True)
            return 1
        except Exception:  # noqa: BLE001
            pass
    for line in lines:
        print(line)
    return 1


def _autostart(action: str) -> int:
    from storage_organizer import autostart
    try:
        if action == "on":
            st = autostart.enable()
        elif action == "off":
            st = autostart.disable()
        elif action == "status":
            st = autostart.status()
        else:
            print("--autostart には on / off / status を指定してください。")
            return 2
    except autostart.Unsupported as e:
        print(e)
        return 2
    except OSError as e:
        print(f"自動起動の設定を変更できませんでした: {e}")
        return 1
    print("ログイン時の自動起動: " + ("有効" if st["enabled"] else "無効"))
    if st.get("command"):
        print("登録されている起動コマンド: " + st["command"])
    if st.get("message"):
        print(st["message"])
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 12):
        sys.exit(_fail(["RecSort には Python 3.12 以上が必要です（今の Python: %d.%d）。" % sys.version_info[:2]]))
    if "--autostart" in sys.argv:
        i = sys.argv.index("--autostart")
        sys.exit(_autostart(sys.argv[i + 1] if i + 1 < len(sys.argv) else ""))
    if "--desktop" in sys.argv:
        from storage_organizer.app import main
        sys.exit(main())
    try:
        import numpy  # noqa: F401  解析に使う部品（初回セットアップで入れる）
    except ImportError:
        sys.exit(_fail(["必要な部品（numpy など）が入っていません。",
                        "RecSortを起動.bat と同じフォルダにある .venv フォルダを削除してから、もう一度ダブルクリックしてください"
                        "（初回セットアップをやり直します）。",
                        "または: python -m pip install -r requirements-web.txt"]))
    from storage_organizer.webapp import main
    sys.exit(main())
