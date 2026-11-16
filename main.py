"""起動用スクリプト: python main.py（ブラウザで開く）。デスクトップ版は python main.py --desktop

Windows では RecSortを起動.bat をダブルクリックすると、このスクリプトがブラウザ版を起動する。
"""

import sys

if __name__ == "__main__":
    if sys.version_info < (3, 12):
        print("RecSort には Python 3.12 以上が必要です（今の Python: %d.%d）。" % sys.version_info[:2])
        sys.exit(1)
    if "--desktop" in sys.argv:
        from storage_organizer.app import main
        sys.exit(main())
    try:
        import numpy  # noqa: F401  解析に使う部品（初回セットアップで入れる）
    except ImportError:
        print("必要な部品（numpy など）が入っていません。")
        print("RecSortを起動.bat と同じフォルダにある .venv フォルダを削除してから、もう一度ダブルクリックしてください"
              "（初回セットアップをやり直します）。")
        print("または: python -m pip install -r requirements-web.txt")
        sys.exit(1)
    from storage_organizer.webapp import main
    sys.exit(main())
