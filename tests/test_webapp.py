"""ブラウザ版（webapp.py）のテスト。

実際のユーザーの録画は使わない。一時フォルダの合成ダミーファイルだけを使う。
ごみ箱への移動は偽の移動処理（名前を変えるだけ）で確かめる。サーバーは 127.0.0.1 の空いている番号で動かす。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import webapp  # noqa: E402
from storage_organizer.store import Store  # noqa: E402


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.videos = self.dir / "videos"
        self.videos.mkdir()
        self.db = Store(self.dir / "data")
        self.moved = []
        self.app = webapp.App(self.db, backend=self.backend)
        self.server = webapp.make_server(self.app, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.a = self.add("a.mp4", b"same" * 1000)
        self.b = self.add("b.mp4", b"same" * 1000)
        self.c = self.add("c.mp4", b"other" * 300)
        sha = hashlib.sha256(b"same" * 1000).hexdigest()
        for m in (self.a, self.b):
            self.db.update_video(m, sha256=sha, light_state="完了")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self._tmp.cleanup()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def add(self, name, data):
        p = self.videos / name
        p.write_bytes(data)
        st = p.stat()
        self.db.upsert_media([(str(p), name, str(self.videos), st.st_size, st.st_mtime, ".mp4", "video", False)], 1,
                             str(self.videos))
        return self.db.id_of(str(p))

    def request(self, path, body=None, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h["X-RecSort-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read()
                return res.status, raw, res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, path, body=None, **kw):
        status, raw, _ = self.request(path, body, **kw)
        return status, json.loads(raw)

    # --- 安全 -----------------------------------------------------------------
    def test_api_requires_token(self):
        status, data = self.json("/api/state", token=False)
        self.assertEqual(status, 403)
        status, _ = self.json("/api/decide", {"ids": [self.c], "decision": "削除候補"}, token=False)
        self.assertEqual(status, 403)
        self.assertIsNone(self.db.video(self.c)["decision"])  # 合言葉のない要求では何も変えない

    def test_other_host_names_are_refused(self):
        # ほかのサイトの名前でこのサーバーに届いた要求（DNS リバインディング）は断る
        status, _ = self.json("/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_index_carries_token_and_static_paths_stay_inside(self):
        status, raw, headers = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token.encode(), raw)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for bad in ("/../webapp.py", "/%2e%2e/webapp.py", "/..%5cwebapp.py"):
            status, _, _ = self.request(bad, token=False)
            self.assertEqual(status, 404, bad)

    def test_media_only_for_registered_videos_and_supports_range(self):
        status, raw, headers = self.request(f"/api/media/{self.c}", headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, (b"other" * 300)[:10])
        self.assertEqual(headers["Content-Range"], f"bytes 0-9/{5 * 300}")
        status, _, _ = self.request("/api/media/99999")
        self.assertEqual(status, 404)
        status, _, _ = self.request(f"/api/media/{self.c}", token=False)
        self.assertEqual(status, 403)

    # --- 画面の情報 -------------------------------------------------------------
    def test_state_files_and_plan(self):
        _, s = self.json("/api/state")
        self.assertEqual(s["videos"], 3)
        self.assertEqual(s["plan_count"], 1)  # 重複の片方だけ（1本は必ず残す）
        _, f = self.json("/api/files")
        self.assertEqual({it["name"] for it in f["items"]}, {"a.mp4", "b.mp4", "c.mp4"})
        _, p = self.json("/api/plan")
        self.assertEqual(len(p["items"]), 1)
        item = p["items"][0]
        self.assertEqual(item["kind"], "duplicate")
        self.assertIn(item["keep_id"], (self.a, self.b))
        self.assertNotEqual(item["id"], item["keep_id"])
        self.assertIn(str(item["keep_id"]), p["rows"])
        _, g = self.json(f"/api/group/{self.a}")
        self.assertEqual(g["kind"], "duplicate")
        self.assertEqual({it["id"] for it in g["items"]}, {self.a, self.b})

    # --- 判断 -----------------------------------------------------------------
    def test_decide_and_protected_is_never_a_candidate(self):
        self.json("/api/protect", {"id": self.c, "value": True})
        self.json("/api/decide", {"ids": [self.a, self.c], "decision": "削除候補"})
        self.assertEqual(self.db.video(self.a)["decision"], "削除候補")
        self.assertIsNone(self.db.video(self.c)["decision"])
        status, _ = self.json("/api/decide", {"ids": [self.a], "decision": "消す"})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.video(self.a)["decision"], "削除候補")  # 知らない値は記録しない

    def test_keep_one_marks_others(self):
        _, r = self.json("/api/keep", {"keep": self.a, "ids": [self.b]})
        self.assertEqual(r["count"], 1)
        self.assertEqual(self.db.video(self.a)["decision"], "残す")
        self.assertEqual(self.db.video(self.b)["decision"], "削除候補")

    # --- ごみ箱 ---------------------------------------------------------------
    def wait_trash(self):
        for _ in range(100):
            _, st = self.json("/api/trash")
            if not st["running"]:
                return st
            time.sleep(0.05)
        self.fail("ごみ箱への移動が終わりません")

    def test_trash_requires_final_confirmation(self):
        self.json("/api/decide", {"ids": [self.b], "decision": "削除候補"})
        status, _ = self.json("/api/trash", {"ids": [self.b]})
        self.assertEqual(status, 400)
        self.assertEqual(self.moved, [])

    def test_trash_moves_checked_files_and_keeps_one_copy(self):
        self.json("/api/decide", {"ids": [self.a, self.b, self.c], "decision": "削除候補"})
        _, chosen = self.json("/api/chosen")
        self.assertEqual(len(chosen["items"]), 3)
        with mock.patch.object(os, "remove", side_effect=AssertionError("完全削除")):
            _, r = self.json("/api/trash", {"ids": [self.a, self.b, self.c], "confirm": True,
                                            "generation": chosen["generation"]})
            self.assertTrue(r["ok"])
            st = self.wait_trash()
        statuses = {x["id"]: x["status"] for x in st["results"]}
        self.assertEqual(statuses[self.c], "moved")
        # 完全に同じ録画を両方選んでも、最後の1本は移動しない
        self.assertEqual(sorted([statuses[self.a], statuses[self.b]]), ["moved", "skipped"])
        self.assertTrue((self.videos / "a.mp4").exists() or (self.videos / "b.mp4").exists())

    def test_trash_stops_if_scan_result_changed(self):
        self.json("/api/decide", {"ids": [self.c], "decision": "削除候補"})
        _, chosen = self.json("/api/chosen")
        self.add("d.mp4", b"new")  # 確認した後に調査結果が変わった
        self.json("/api/trash", {"ids": [self.c], "confirm": True, "generation": chosen["generation"]})
        st = self.wait_trash()
        self.assertEqual([x["status"] for x in st["results"]], ["skipped"])
        self.assertEqual(self.moved, [])

    def test_settings_reject_relative_paths(self):
        _, r = self.json("/api/settings", {"extra_locations": ["relative\\path"]})
        self.assertFalse(r["ok"])
        _, s = self.json("/api/settings")
        self.assertEqual(s["extra_locations"], [])
        path = str(self.dir)
        _, s = self.json("/api/settings", {"extra_locations": [path], "auto_scan": False})
        self.assertEqual(s["extra_locations"], [path])
        self.assertFalse(s["auto_scan"])


    # --- 調べるフォルダの指定 -------------------------------------------------------
    def wait_scan(self):
        for _ in range(600):
            _, st = self.json("/api/state")
            if not st["scanning"]:
                return st
            time.sleep(0.05)
        self.fail("調査が終わりません")

    def test_check_folder_reports_reasons(self):
        _, r = self.json("/api/folder/check", {"path": ""})
        self.assertIn("入力", r["error"])
        _, r = self.json("/api/folder/check", {"path": "relative\\path"})
        self.assertIn("フルパス", r["error"])
        _, r = self.json("/api/folder/check", {"path": str(self.dir / "ない フォルダ")})
        self.assertIn("見つかりません", r["error"])
        _, r = self.json("/api/folder/check", {"path": str(self.videos / "c.mp4")})
        self.assertIn("フォルダではありません", r["error"])
        with mock.patch.object(webapp.os, "scandir", side_effect=PermissionError(13, "Access is denied")):
            _, r = self.json("/api/folder/check", {"path": str(self.videos)})
        self.assertIn("権限がありません", r["error"])
        _, r = self.json("/api/folder/check", {"path": f'"{self.videos}"'})  # エクスプローラーの「パスのコピー」
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], str(self.videos))

    def test_scan_specified_folder_with_japanese_and_spaces(self):
        folder = self.dir / "録画 フォルダ" / "ゲーム 録画"
        folder.mkdir(parents=True)
        (folder / "試合 01.mp4").write_bytes(b"x" * 2000)
        (folder / "試合 01 - コピー.mp4").write_bytes(b"x" * 2000)
        (folder / "メモ.txt").write_bytes(b"memo")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        with mock.patch.object(webapp.media, "find_ffmpeg", return_value=webapp.media.FFmpegTools(None, None, "")):
            status, r = self.json("/api/scan", {"folders": [str(folder)]})
            self.assertEqual((status, r["ok"]), (200, True))
            _, again = self.json("/api/scan", {})  # 連打・再読み込みでも二重に始めない
            self.assertTrue(again["ok"])
            st = self.wait_scan()
        self.assertEqual(st["scan_folders"], [str(folder)])
        self.assertIsNone((st["scan_result"] or {}).get("error"))
        self.assertFalse(st["ffmpeg"])
        _, f = self.json("/api/files")
        found = {it["name"] for it in f["items"] if it["folder"] == str(folder)}
        self.assertEqual(found, {"試合 01.mp4", "試合 01 - コピー.mp4"})  # 録画だけを登録する（.txt は入れない）
        _, p = self.json("/api/plan")
        dup = [it for it in p["items"] if p["rows"][str(it["id"])]["folder"] == str(folder)]
        self.assertEqual([it["kind"] for it in dup], ["duplicate"])
        self.assertTrue(dup[0]["reasons"])
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        self.assertEqual(before, after)  # 録画は読み取るだけ

    def test_scan_refuses_missing_folder_and_keeps_settings(self):
        status, r = self.json("/api/scan", {"folders": [str(self.dir / "ない")]})
        self.assertFalse(r["ok"])
        self.assertIn("見つかりません", r["error"])
        self.assertEqual(self.db.json_list("scan_folders"), [])
        self.assertFalse(self.app.scan_thread and self.app.scan_thread.is_alive())
        gone = self.dir / "あとで消える"
        gone.mkdir()
        self.db.set_json_list("scan_folders", [str(gone)])
        gone.rmdir()  # 外付けドライブを外した・名前を変えた
        _, r = self.json("/api/scan", {})
        self.assertFalse(r["ok"])
        _, st = self.json("/api/state")
        self.assertIn("見つかりません", st["scan_result"]["error"])

    def test_scan_is_not_started_twice(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*a, **kw):
            started.set()
            release.wait(5)
            return {"cancelled": False}
        with mock.patch.object(webapp.autoscan, "task_autoscan", side_effect=slow) as task:
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.json("/api/scan", {})[1]))
                       for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            started.wait(5)
            release.set()
            self.wait_scan()
        self.assertEqual(task.call_count, 1)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sum(1 for r in results if r.get("already_running")), 5)

    def test_health_needs_no_token_and_reveals_nothing(self):
        status, r = self.json("/api/health", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(r["service"], "recsort")
        self.assertEqual(set(r), {"app", "service", "version"})
        self.assertEqual(webapp.probe(self.server.server_address[1]), "recsort")

    def test_static_and_video_types_do_not_depend_on_registry(self):
        with mock.patch.object(webapp.mimetypes, "guess_type", return_value=("text/plain", None)):
            _, _, headers = self.request("/app.js", token=False)
            self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
            _, _, headers = self.request(f"/api/media/{self.c}")
            self.assertEqual(headers["Content-Type"], "video/mp4")


class StartupPortTest(unittest.TestCase):
    """二重起動・ポートの取り合い（ブラウザは開かない）。"""

    def test_probe_tells_recsort_other_app_and_free_port(self):
        import socket
        from http.server import BaseHTTPRequestHandler, HTTPServer
        with socket.socket() as s:  # 何も待ち受けていない番号
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.assertEqual(webapp.probe(free), "none")

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>other app</html>")

            def log_message(self, *a):
                pass
        other = HTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertEqual(webapp.probe(other.server_address[1]), "other")
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RECSORT_DATA_DIR": tmp}), \
                    mock.patch("builtins.print") as out:
                code = webapp.main(["--no-browser", "--no-scan", "--port", str(other.server_address[1])])
            self.assertEqual(code, 3)
            self.assertIn("別のアプリ", " ".join(str(c.args[0]) for c in out.call_args_list))
        finally:
            other.shutdown()
            other.server_close()

    def test_second_launch_opens_running_recsort(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(Path(tmp) / "data")
            server = webapp.make_server(webapp.App(db), 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                with mock.patch.object(webapp.webbrowser, "open", return_value=True) as opened, \
                        mock.patch("builtins.print"):
                    code = webapp.main(["--port", str(port)])
                self.assertEqual(code, 0)
                opened.assert_called_once_with(f"http://127.0.0.1:{port}/")
            finally:
                server.shutdown()
                server.server_close()
                db.close()

if __name__ == "__main__":
    unittest.main()
