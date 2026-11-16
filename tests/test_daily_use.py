"""普段使いのための改善のテスト（自動起動・起動と再起動・類似録画の見分け方・類似と完全一致の操作の違い）。

実際の録画・既存の設定・本物のレジストリは使わない。一時フォルダの合成動画と、偽の登録先（FakeRegistry）だけを使う。
FFmpeg がない環境では、合成動画を使うテストだけを skip する。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import autostart, candidates, media, similar, webapp  # noqa: E402
from storage_organizer.instance_lock import InstanceLock  # noqa: E402
from storage_organizer.store import Store  # noqa: E402

TOOLS = media.find_ffmpeg()
needs_ffmpeg = unittest.skipUnless(TOOLS.available, "FFmpeg がない環境")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeRegistry:
    """HKCU\\...\\Run の代わり（ほかのアプリの登録も入れて、RecSort の値だけを扱うか確かめる）。"""

    def __init__(self, values=None, fail=False):
        self.values = dict(values or {})
        self.fail = fail

    def get(self):
        if self.fail:
            raise PermissionError(5, "アクセスが拒否されました")
        return self.values.get(autostart.VALUE_NAME)

    def set(self, command):
        if self.fail:
            raise PermissionError(5, "アクセスが拒否されました")
        self.values[autostart.VALUE_NAME] = command

    def delete(self):
        if self.fail:
            raise PermissionError(5, "アクセスが拒否されました")
        self.values.pop(autostart.VALUE_NAME, None)


# =============================================================================
# 自動起動の登録（偽の登録先）
# =============================================================================
class AutostartTest(unittest.TestCase):
    def test_not_supported_outside_windows(self):
        with mock.patch.object(autostart.os, "name", "posix"):
            st = autostart.status()
        self.assertFalse(st["supported"])
        self.assertFalse(st["enabled"])
        self.assertIn("Windows", st["message"])

    def test_enable_registers_background_command_with_quotes(self):
        reg = FakeRegistry({"OtherApp": "other.exe"})
        st = autostart.enable(reg)
        cmd = reg.values[autostart.VALUE_NAME]
        self.assertTrue(st["enabled"] and st["current"])
        self.assertIn("--background", cmd)
        self.assertIn(f'"{autostart.MAIN}"', cmd)  # 日本語・空白を含む場所でも動くよう引用符で囲む
        self.assertTrue(cmd.startswith('"'))
        self.assertNotIn("--port", cmd)
        self.assertEqual(reg.values["OtherApp"], "other.exe")

    def test_disable_removes_only_recsort(self):
        reg = FakeRegistry({"OtherApp": "other.exe"})
        autostart.enable(reg)
        st = autostart.disable(reg)
        self.assertFalse(st["enabled"])
        self.assertEqual(reg.values, {"OtherApp": "other.exe"})
        autostart.disable(reg)  # 2回解除してもエラーにしない
        self.assertEqual(reg.values, {"OtherApp": "other.exe"})

    def test_status_warns_when_folder_moved(self):
        reg = FakeRegistry({autostart.VALUE_NAME: '"C:\\old place\\pythonw.exe" "C:\\old place\\main.py" --background'})
        st = autostart.status(reg)
        self.assertTrue(st["enabled"])
        self.assertFalse(st["current"])
        self.assertIn("場所と違います", st["message"])

    def test_non_default_port_is_kept(self):
        reg = FakeRegistry()
        autostart.enable(reg, port=8766)
        self.assertTrue(reg.values[autostart.VALUE_NAME].endswith("--background --port 8766"))

    def test_background_python_prefers_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "録画 整理" / "Scripts"
            d.mkdir(parents=True)
            (d / "python.exe").write_bytes(b"")
            self.assertEqual(autostart.background_python(str(d / "python.exe")), str(d / "python.exe"))
            (d / "pythonw.exe").write_bytes(b"")
            self.assertEqual(autostart.background_python(str(d / "python.exe")), str(d / "pythonw.exe"))


class ServerCase(unittest.TestCase):
    """一時フォルダの保存データでサーバーを動かす（ブラウザは開かない）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.videos = self.dir / "録画 フォルダ"
        self.videos.mkdir()
        self.db = Store(self.dir / "data")
        self.moved = []
        self.app = webapp.App(self.db, backend=self.backend)
        self.app.autostart_registry = FakeRegistry({"OtherApp": "other.exe"})
        self.server = webapp.make_server(self.app, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self._tmp.cleanup()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def add(self, name, data=b"x" * 100, **fields):
        p = self.videos / name
        if not p.exists():
            p.write_bytes(data)
        st = p.stat()
        self.db.upsert_media([(str(p), name, str(self.videos), st.st_size, st.st_mtime, p.suffix, "video", False)], 1,
                             str(self.videos))
        mid = self.db.id_of(str(p))
        if fields:
            self.db.update_video(mid, **fields)
        return mid

    def request(self, path, body=None, token=True, headers=None, method=None):
        h = dict(headers or {})
        if token:
            h["X-RecSort-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, path, body=None, **kw):
        status, raw, _ = self.request(path, body, **kw)
        return status, json.loads(raw)


class AutostartApiTest(ServerCase):
    def test_api_toggle_and_status(self):
        _, st = self.json("/api/autostart")
        self.assertTrue(st["ok"] and st["supported"])
        self.assertFalse(st["enabled"])
        _, st = self.json("/api/autostart", {"enabled": True})
        self.assertTrue(st["enabled"])
        self.assertIn("--background", self.app.autostart_registry.values["RecSort"])
        _, st = self.json("/api/autostart", {"enabled": False})
        self.assertFalse(st["enabled"])
        self.assertEqual(self.app.autostart_registry.values, {"OtherApp": "other.exe"})

    def test_api_requires_token_and_bool(self):
        status, _ = self.json("/api/autostart", {"enabled": True}, token=False)
        self.assertEqual(status, 403)
        status, _ = self.json("/api/autostart", {"enabled": "yes"})
        self.assertEqual(status, 400)
        status, _ = self.json("/api/autostart", {})
        self.assertEqual(status, 400)
        self.assertNotIn("RecSort", self.app.autostart_registry.values)  # 明示的な操作がなければ登録しない

    def test_api_reports_registry_errors(self):
        self.app.autostart_registry = FakeRegistry(fail=True)
        _, st = self.json("/api/autostart", {"enabled": True})
        self.assertFalse(st["ok"])
        self.assertIn("登録できませんでした", st["error"])
        _, st = self.json("/api/autostart")
        self.assertFalse(st["ok"])

    def test_api_unsupported_platform(self):
        self.app.autostart_registry = None
        with mock.patch.object(autostart.os, "name", "posix"):
            _, st = self.json("/api/autostart", {"enabled": True})
        self.assertFalse(st["ok"])
        self.assertIn("Windows", st["error"])

    def test_port_registered_when_not_default(self):
        self.app.port = 8790
        self.json("/api/autostart", {"enabled": True})
        self.assertIn("--port 8790", self.app.autostart_registry.values["RecSort"])

    def test_shutdown_needs_confirmation_and_not_while_trashing(self):
        stopped = []
        self.app.shutdown = lambda: stopped.append(True)
        status, _ = self.json("/api/shutdown", {})
        self.assertEqual(status, 400)
        self.app.trash_job = {"done": False}
        _, r = self.json("/api/shutdown", {"confirm": True})
        self.assertFalse(r["ok"])
        self.app.trash_job = None
        _, r = self.json("/api/shutdown", {"confirm": True})
        self.assertTrue(r["ok"])
        self.assertEqual(stopped, [True])


# =============================================================================
# 起動・再起動・二重起動・ポート競合（別プロセス。ブラウザの代わりに記録するだけのコマンド）
# =============================================================================
class StartupProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "保存 データ"
        self.browser_log = self.dir / "browser.log"
        fake = self.dir / "fake-browser.py"
        fake.write_text(f"import sys\nopen({str(self.browser_log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
        self.env = dict(os.environ, RECSORT_DATA_DIR=str(self.data), BROWSER=f"{sys.executable} {fake} %s")
        self.port = free_port()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)
        self._tmp.cleanup()

    def start(self, *args):
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--port", str(self.port), "--no-scan", *args],
                             cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def run_main(self, *args, timeout=40):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), "--port", str(self.port), "--no-scan", *args],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)

    def wait_up(self):
        self.assertTrue(webapp.wait_ready(self.port, 20), "サーバーが起動しません")

    def browser_calls(self):
        return self.browser_log.read_text().splitlines() if self.browser_log.exists() else []

    def stop_via_api(self):
        html = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        token = html.split('name="recsort-token" content="')[1].split('"')[0]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/shutdown", data=b'{"confirm": true}',
                                     headers={"X-RecSort-Token": token, "Content-Type": "application/json"})
        self.assertTrue(json.loads(urllib.request.urlopen(req, timeout=10).read())["ok"])

    def test_background_starts_server_only(self):
        p = self.start("--background")
        self.wait_up()
        time.sleep(0.5)
        self.assertEqual(self.browser_calls(), [])  # ログインのたびにブラウザを開かない
        log = (self.data / "server.log").read_text(encoding="utf-8")
        self.assertIn("ログイン時の自動起動", log)
        self.stop_via_api()
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertIn("終了しました", (self.data / "server.log").read_text(encoding="utf-8"))

    def test_manual_start_opens_browser_after_ready(self):
        self.start()
        self.wait_up()
        for _ in range(50):
            if self.browser_calls():
                break
            time.sleep(0.1)
        self.assertEqual(self.browser_calls(), [f"http://127.0.0.1:{self.port}/"])

    def test_second_launch_while_background_running(self):
        self.start("--background")
        self.wait_up()
        r = self.run_main("--background")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.browser_calls(), [])
        r = self.run_main()  # バッチから手動で起動 → 起動中の RecSort を開くだけ
        self.assertEqual(r.returncode, 0)
        self.assertIn("すでに起動しています", r.stdout)
        self.assertEqual(self.browser_calls(), [f"http://127.0.0.1:{self.port}/"])

    def test_restart_keeps_settings_and_decisions(self):
        vids = self.dir / "録画 A"
        vids.mkdir()
        (vids / "試合 1.mp4").write_bytes(b"v" * 500)
        p = self.start("--no-browser")
        self.wait_up()
        html = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        token = html.split('name="recsort-token" content="')[1].split('"')[0]

        def call(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-RecSort-Token": token, "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        self.assertTrue(call("/api/scan", {"folders": [str(vids)]})["ok"])
        for _ in range(200):
            if not call("/api/state")["scanning"]:
                break
            time.sleep(0.1)
        mid = call("/api/files")["items"][0]["id"]
        call("/api/decide", {"ids": [mid], "decision": "保留"})
        call("/api/protect", {"id": mid, "value": True})
        self.stop_via_api()
        self.assertEqual(p.wait(timeout=20), 0)
        self.start("--no-browser")  # 同じ番号で再起動できる
        self.wait_up()
        html = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        token = html.split('name="recsort-token" content="')[1].split('"')[0]
        state = call("/api/state")
        self.assertEqual(state["scan_folders"], [str(vids)])
        item = call("/api/files")["items"][0]
        self.assertEqual((item["name"], item["decision"], item["protected"]), ("試合 1.mp4", "保留", True))

    def test_port_used_by_other_app_in_background(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", self.port))
            other.listen()
            r = self.run_main("--background")
        self.assertEqual(r.returncode, 3)
        log = (self.data / "server.log").read_text(encoding="utf-8")
        self.assertIn("別のアプリが使用", log)
        self.assertIn("--port 8766", log)  # 直し方も書く

    def test_lock_held_without_server(self):
        self.data.mkdir(parents=True)
        lock = InstanceLock(self.data).acquire()  # デスクトップ版などが保存データを使っている
        try:
            r = self.run_main("--no-browser")
        finally:
            lock.release()
        self.assertEqual(r.returncode, 1)
        self.assertIn("同時に2つは起動できません", r.stdout)

    def test_invalid_port_argument(self):
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--port", "abc"], cwd=ROOT, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--port", r.stdout)

    def test_autostart_cli_status_outside_windows(self):
        if os.name == "nt":
            self.skipTest("Windows では本物の登録先を読むため、このテストは行わない")
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--autostart", "status"], cwd=ROOT, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("無効", r.stdout)
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--autostart", "on"], cwd=ROOT, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)


# =============================================================================
# 類似と完全一致の操作の違い
# =============================================================================
def fake_sig(seed: int, duration: float = 20.0, differ: tuple = ()) -> dict:
    rng = np.random.default_rng(seed)
    frames = []
    for i in range(similar.N_POINTS):
        f = rng.normal(128, 40, (similar.H, similar.W))  # 違う場面でも同じだけ乱数を使い、ほかの場面をずらさない
        if i in differ:
            f = np.random.default_rng(1000 + seed * 31 + i).normal(128, 40, (similar.H, similar.W))
        frames.append(base64.b64encode(np.clip(f, 0, 255).astype(np.uint8).tobytes()).decode())
    return {"v": similar.VERSION, "duration": duration, "frames": frames}


class SimilarVsDuplicateTest(ServerCase):
    def setUp(self):
        super().setUp()
        meta = {"duration": 20.0, "width": 640, "height": 360, "has_audio": True}
        self.s1 = self.add("似た 1.mp4", b"a" * 300, meta=meta, vsig=fake_sig(1), light_state="完了")
        self.s2 = self.add("似た 2.mp4", b"b" * 400, meta=meta, vsig=fake_sig(1, differ=(5,)), light_state="完了")
        self.s3 = self.add("似た 3.mp4", b"c" * 500, meta=meta, vsig=fake_sig(1), light_state="完了")
        self.db.set_video_groups([[self.s1, self.s2, self.s3]])
        sha = hashlib.sha256(b"same" * 50).hexdigest()
        self.d1 = self.add("コピー 1.mp4", b"same" * 50, sha256=sha, light_state="完了")
        self.d2 = self.add("コピー 2.mp4", b"same" * 50, sha256=sha, light_state="完了")
        self.d3 = self.add("コピー 3.mp4", b"same" * 50, sha256=sha, light_state="完了")

    def test_keep_in_similar_group_changes_nothing_else(self):
        _, r = self.json("/api/keep", {"keep": self.s1, "ids": [self.s2, self.s3]})  # 古い画面が送っても
        self.assertEqual((r["kind"], r["count"]), ("similar", 0))
        self.assertEqual(self.db.video(self.s1)["decision"], "残す")
        self.assertIsNone(self.db.video(self.s2)["decision"])
        self.assertIsNone(self.db.video(self.s3)["decision"])

    def test_similar_candidates_are_chosen_one_by_one(self):
        self.json("/api/keep", {"keep": self.s1, "ids": []})
        self.json("/api/decide", {"ids": [self.s2], "decision": "削除候補"})
        self.assertEqual(self.db.video(self.s2)["decision"], "削除候補")
        self.assertIsNone(self.db.video(self.s3)["decision"])
        _, plan = self.json("/api/plan")
        sim = [it for it in plan["items"] if it["kind"] == "similar"]
        self.assertTrue(all(it["confirmed"] for it in sim))  # 残す1本が決まったので選べるようになる

    def test_keep_in_duplicate_group_marks_only_copies(self):
        _, r = self.json("/api/keep", {"keep": self.d1, "ids": [self.d2, self.d3, self.s2]})
        self.assertEqual((r["kind"], r["count"]), ("duplicate", 2))
        self.assertEqual(self.db.video(self.d2)["decision"], "削除候補")
        self.assertEqual(self.db.video(self.d3)["decision"], "削除候補")
        self.assertIsNone(self.db.video(self.s2)["decision"])  # グループ外の ID は無視する

    def test_keep_duplicate_skips_protected_copy(self):
        self.db.set_protected(self.d2, True)
        _, r = self.json("/api/keep", {"keep": self.d1, "ids": [self.d2, self.d3]})
        self.assertEqual(r["count"], 1)
        self.assertIsNone(self.db.video(self.d2)["decision"])

    def test_keep_unknown_recording(self):
        _, r = self.json("/api/keep", {"keep": 99999, "ids": []})
        self.assertFalse(r["ok"])

    def test_protected_similar_never_becomes_candidate(self):
        self.db.set_protected(self.s3, True)
        self.json("/api/decide", {"ids": [self.s3], "decision": "削除候補"})
        self.assertIsNone(self.db.video(self.s3)["decision"])

    def test_group_api_lists_differing_scenes(self):
        _, g = self.json(f"/api/group/{self.s2}")
        self.assertEqual(g["kind"], "similar")
        self.assertEqual(len(g["scenes"]), similar.N_POINTS)
        self.assertEqual(g["differ_scenes"], [5])
        ref = str(g["reference_id"])
        self.assertTrue(all(sc["status"][ref] == "ref" for sc in g["scenes"]))
        self.assertAlmostEqual(g["scenes"][5]["time"], 20.0 * 5.5 / similar.N_POINTS, places=1)
        self.assertIn("音声は自動では比べていません", g["audio_note"])
        self.assertEqual(set(g["has_audio"]), {str(self.s1), str(self.s2), str(self.s3)})

    def test_trash_cancelled_by_user_moves_nothing(self):
        # 最終確認で「いいえ」を押した場合は、移動の要求自体が送られない（承認のない要求はサーバーが断る）
        self.json("/api/decide", {"ids": [self.d2], "decision": "削除候補"})
        status, _ = self.json("/api/trash", {"ids": [self.d2], "confirm": False})
        self.assertEqual(status, 400)
        self.assertEqual(self.moved, [])
        self.assertTrue((self.videos / "コピー 2.mp4").exists())

    def test_trash_cancel_request_stops_before_next_file(self):
        self.json("/api/decide", {"ids": [self.d2, self.d3], "decision": "削除候補"})
        _, chosen = self.json("/api/chosen")
        started = threading.Event()
        release = threading.Event()
        original = self.backend

        def slow(path):
            started.set()
            release.wait(5)
            original(path)
        self.app.backend = slow
        self.json("/api/trash", {"ids": [self.d2, self.d3], "confirm": True, "generation": chosen["generation"]})
        started.wait(5)
        self.json("/api/trash/cancel", {})
        release.set()
        for _ in range(100):
            _, st = self.json("/api/trash")
            if not st["running"]:
                break
            time.sleep(0.05)
        self.assertLessEqual(len(self.moved), 1)  # 処理中の1本が終わったところで止まる
        self.assertTrue((self.videos / "コピー 1.mp4").exists())


# =============================================================================
# 類似判定（基準そのもの）: 合成した代表場面で
# =============================================================================
class CompareRulesTest(unittest.TestCase):
    def test_two_differing_scenes_are_not_similar(self):
        r = similar.compare(fake_sig(3), fake_sig(3, differ=(4, 5)))
        self.assertEqual(r["verdict"], "different")
        self.assertEqual(r["matched"], 10)  # 10/12 は以前の基準（75%）では「類似」だった

    def test_one_differing_scene_is_still_similar(self):
        r = similar.compare(fake_sig(3), fake_sig(3, differ=(7,)))
        self.assertEqual(r["verdict"], "similar")
        self.assertEqual(r["per_ok"].count(False), 1)

    def test_black_frames_stay_uncomparable(self):
        black = base64.b64encode(np.zeros(similar.W * similar.H, np.uint8)).decode()
        sig = {"v": 1, "duration": 20.0, "frames": [black] * similar.N_POINTS}
        r = similar.compare(sig, sig)
        self.assertEqual((r["verdict"], r["informative"]), ("unknown", 0))

    def test_low_contrast_uses_stricter_threshold(self):
        rng = np.random.default_rng(5)
        base = rng.normal(0, 3, (similar.N_POINTS, similar.H * similar.W))
        enc = lambda arr: [base64.b64encode(np.clip(f + 60, 0, 255).astype(np.uint8).tobytes()).decode() for f in arr]
        a = {"v": 1, "duration": 20.0, "frames": enc(base)}
        b_close = {"v": 1, "duration": 20.0, "frames": enc(base + rng.normal(0, 0.5, base.shape))}
        b_far = {"v": 1, "duration": 20.0, "frames": enc(base * 0.6 + rng.normal(0, 2.4, base.shape))}
        self.assertEqual(similar.compare(a, b_close)["verdict"], "similar")
        far = similar.compare(a, b_far)
        self.assertEqual(far["verdict"], "different")  # 相関 0.85〜0.9 は、明暗差の小さい場面では一致としない
        self.assertTrue(all(c is None or c < similar.LOW_MATCH_CORR for c in far["per_frame"]))

    def test_pair_detail_follows_per_scene_verdict(self):
        row = lambda sig: {"vsig": json.dumps(sig)}
        d = candidates.similar_pair_detail(row(fake_sig(4)), row(fake_sig(4, differ=(2,))))
        self.assertEqual(d["unmatched"], [2])
        self.assertEqual(len(d["matched"]), similar.N_POINTS - 1)
        d = candidates.similar_pair_detail(row(fake_sig(4)), row(fake_sig(4, duration=60.0)))
        self.assertEqual(len(d["uncomparable"]), similar.N_POINTS)  # 長さが違えば比べない


# =============================================================================
# 類似判定の再現テスト（合成動画）: 前回の検証で見つかった問題
# =============================================================================
@needs_ffmpeg
class SimilarVideosTest(unittest.TestCase):
    S = "s=160x90:r=10"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name) / "似た 録画"
        cls.dir.mkdir()
        S = cls.S

        def ff(*args):
            subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", *args], check=True)

        def clip(name, src, sec, *extra):
            ff("-f", "lavfi", "-i", src, "-t", str(sec), "-pix_fmt", "yuv420p", "-c:v", "libx264", *extra,
               str(cls.dir / name))
            return cls.dir / name

        def concat(name, parts):
            lst = cls.dir / f"{name}.txt"
            lst.write_text("".join(f"file '{p}'\n" for p in parts))
            ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-pix_fmt", "yuv420p",
               str(cls.dir / name))
        head, tail = clip("_head.mp4", f"smptehdbars={S}", 8), clip("_tail.mp4", f"testsrc={S}", 8)
        concat("回 1.mp4", [head, clip("_b1.mp4", f"life={S}:mold=10", 4), tail])
        concat("回 2.mp4", [head, clip("_b2.mp4", f"cellauto={S}:rule=110", 4), tail])
        for name, freq in (("音 440.mp4", 440), ("音 880.mp4", 880)):
            ff("-f", "lavfi", "-i", f"testsrc2={S}", "-f", "lavfi", "-i", f"sine=frequency={freq}:beep_factor=4",
               "-t", "10", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", str(cls.dir / name))
        ff("-i", str(cls.dir / "音 440.mp4"), "-c:v", "libx264", "-crf", "35", "-c:a", "aac", "-b:a", "48k",
           str(cls.dir / "音 440 再圧縮.mp4"))
        clip("音なし.mp4", f"testsrc2={S}", 10)
        clip("暗い.mp4", f"testsrc2={S},eq=contrast=0.08:brightness=-0.35", 12)
        ff("-i", str(cls.dir / "暗い.mp4"), "-vf", "scale=96:54", "-c:v", "libx264", "-crf", "28",
           str(cls.dir / "暗い 縮小.mp4"))
        clip("暗いノイズ 1.mp4", f"nullsrc={S},geq=random(1)*12+20:128:128", 12)
        clip("暗いノイズ 2.mp4", f"nullsrc={S},geq=random(1)*12+20:128:128,hue=s=0", 12)
        clip("黒.mp4", f"color=c=black:{S}", 12)
        clip("黒 2.mp4", f"color=c=black:{S}", 12)
        cls.sigs = {}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def sig(self, name):
        if name not in self.sigs:
            p = str(self.dir / name)
            self.sigs[name] = similar.signature(TOOLS, p, media.probe(TOOLS, p)["duration"])
        return self.sigs[name]

    def compare(self, a, b, before=False):
        if not before:
            return similar.compare(self.sig(a), self.sig(b))
        with mock.patch.multiple(similar, LOW_STD=similar.FLAT_STD, MAX_UNMATCHED=similar.N_POINTS):
            return similar.compare(self.sig(a), self.sig(b))

    def test_common_16_of_20_seconds_was_grouped_before(self):
        self.assertEqual(self.compare("回 1.mp4", "回 2.mp4", before=True)["verdict"], "similar")  # 再現

    def test_common_16_of_20_seconds_is_not_similar_now(self):
        r = self.compare("回 1.mp4", "回 2.mp4")
        self.assertEqual(r["verdict"], "different")
        self.assertGreaterEqual(r["per_ok"].count(False), 2)  # 違う場面を比較画面で示せる

    def test_audio_only_difference_video_still_similar(self):
        self.assertEqual(self.compare("音 440.mp4", "音 880.mp4")["verdict"], "similar")  # 映像は同じ

    def test_audio_compare_detects_different_audio(self):
        r = similar.audio_compare(TOOLS, str(self.dir / "音 440.mp4"), str(self.dir / "音 880.mp4"), 10)
        self.assertEqual(r["verdict"], "different")
        self.assertGreater(r["compared"], 0)

    def test_audio_compare_same_audio_after_recompress(self):
        r = similar.audio_compare(TOOLS, str(self.dir / "音 440.mp4"), str(self.dir / "音 440 再圧縮.mp4"), 10)
        self.assertEqual(r["verdict"], "same")

    def test_audio_compare_without_audio_track(self):
        r = similar.audio_compare(TOOLS, str(self.dir / "音 440.mp4"), str(self.dir / "音なし.mp4"), 10)
        self.assertEqual(r["verdict"], "no_audio")
        self.assertIn("音声がない", r["reason"])

    def test_audio_compare_without_ffmpeg(self):
        r = similar.audio_compare(media.FFmpegTools(None, None, ""), "a", "b", 10)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("FFmpeg", r["reason"])

    def test_low_contrast_was_missed_before(self):
        self.assertEqual(self.compare("暗い.mp4", "暗い 縮小.mp4", before=True)["verdict"], "unknown")  # 再現

    def test_low_contrast_found_now(self):
        self.assertEqual(self.compare("暗い.mp4", "暗い 縮小.mp4")["verdict"], "similar")

    def test_dark_noise_videos_not_grouped(self):
        self.assertNotEqual(self.compare("暗いノイズ 1.mp4", "暗いノイズ 2.mp4")["verdict"], "similar")

    def test_black_videos_not_grouped(self):
        self.assertEqual(self.compare("黒.mp4", "黒 2.mp4")["verdict"], "unknown")

    def test_files_are_not_modified(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dir.glob("*.mp4")}
        for n in ("回 1.mp4", "音 440.mp4", "暗い.mp4"):
            self.sig(n)
        similar.audio_compare(TOOLS, str(self.dir / "音 440.mp4"), str(self.dir / "音 880.mp4"), 10)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dir.glob("*.mp4")}
        self.assertEqual(before, after)


@needs_ffmpeg
class CompareScreenApiTest(ServerCase):
    """比較画面の API（場面の画像・音声の比較）を、日本語と空白を含むパスの合成動画で。"""

    def setUp(self):
        super().setUp()
        S = "s=160x90:r=10"
        for name, freq in (("試合 音A.mp4", 440), ("試合 音B.mp4", 880)):
            subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2={S}", "-f", "lavfi",
                            "-i", f"sine=frequency={freq}", "-t", "6", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                            "-c:a", "aac", str(self.videos / name)], check=True)
        self.a = self.add("試合 音A.mp4", meta={"duration": 6.0, "has_audio": True})
        self.b = self.add("試合 音B.mp4", meta={"duration": 6.0, "has_audio": True})

    def test_scene_image_for_registered_video(self):
        status, raw, headers = self.request(f"/api/scene/{self.a}/3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(raw[:3], b"\xff\xd8\xff")
        status, raw2, _ = self.request(f"/api/scene/{self.a}/3")  # 2回目はキャッシュ
        self.assertEqual(raw, raw2)

    def test_scene_image_rejects_bad_requests(self):
        self.assertEqual(self.request(f"/api/scene/{self.a}/{similar.N_POINTS}")[0], 404)
        self.assertEqual(self.request("/api/scene/99999/0")[0], 404)
        self.assertEqual(self.request(f"/api/scene/{self.a}/0", token=False)[0], 403)

    def test_audio_api_reports_difference(self):
        _, r = self.json(f"/api/audio/{self.a}/{self.b}")
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "different")
        _, r = self.json("/api/audio/99999/1")
        self.assertFalse(r["ok"])

    def test_media_head_and_range_with_japanese_name(self):
        status, raw, headers = self.request(f"/api/media/{self.a}", method="HEAD")
        self.assertEqual((status, raw), (200, b""))
        size = int(headers["Content-Length"])
        status, raw, headers = self.request(f"/api/media/{self.a}", headers={"Range": "bytes=-100"})
        self.assertEqual(status, 206)
        self.assertEqual(len(raw), 100)
        self.assertEqual(headers["Content-Range"], f"bytes {size - 100}-{size - 1}/{size}")
        self.assertEqual(self.request(f"/api/media/{self.a}", headers={"Range": f"bytes={size}-"})[0], 416)


if __name__ == "__main__":
    unittest.main()
