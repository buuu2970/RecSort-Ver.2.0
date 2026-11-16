"""ブラウザで使う RecSort（このパソコンの中だけで動く小さなサーバー）。

- 画面はブラウザ（web_ui/）。調査・候補・保存・ごみ箱への移動は、デスクトップ版と同じ処理をそのまま使う
- 127.0.0.1 だけで待ち受ける（ほかのパソコンからは開けない）。起動ごとに作る合言葉（トークン）がない要求は断る。
  ほかのサイトのページから、こっそり API を呼ばれないようにするため（Host ヘッダーも確認する）
- 録画の内容を外部へ送信しない。標準ライブラリだけで動く（Flask などは使わない）
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import APP_NAME, __version__, autoscan, candidates, media, similar, trash
from .store import Store, StoreError

UI_DIR = Path(__file__).resolve().parent / "web_ui"
DECISIONS = {"残す", "保留", "削除候補", None}
DEFAULT_PORT = 8765
HEALTH_PATH = "/api/health"
# Windows ではレジストリの設定で .js が text/plain になることがある（nosniff で読み込まれなくなる）ため、固定で決める
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon"}
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
               ".mkv": "video/x-matroska", ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
               ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
               ".mpeg": "video/mpeg", ".3gp": "video/3gpp", ".ogv": "video/ogg"}


def check_folder(raw) -> tuple[str | None, str | None]:
    """画面で入力されたフォルダを確かめる。(正規化したパス, 問題があればその理由)。

    中身は読み取るだけで、変更しない。エクスプローラーの「パスのコピー」で付く前後の " は外す。
    """
    text = str(raw or "").strip().strip('"').strip()
    if not text:
        return None, "フォルダのパスを入力してください"
    if not os.path.isabs(text) or (os.name == "nt" and not os.path.splitdrive(text)[0]):
        return None, f"フルパスで指定してください（例: D:\\録画）: {text}"
    path = os.path.normpath(text)
    if not os.path.exists(path):
        return None, f"フォルダが見つかりません: {path}"
    if not os.path.isdir(path):
        return None, f"フォルダではありません（ファイルが指定されています）: {path}"
    try:
        with os.scandir(path) as it:
            next(it, None)
    except PermissionError:
        return None, f"フォルダを読み取る権限がありません: {path}"
    except OSError as e:
        return None, f"フォルダを読み取れません（{e.strerror or type(e).__name__}）: {path}"
    return path, None


# フォルダを選ぶ Windows の画面（tkinter）を別プロセスで開く。結果は JSON（ASCII）で受け取る
_PICKER = r"""
import json, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(parent=root, title="録画のフォルダを選択", mustexist=True)
root.destroy()
print(json.dumps(path or ""))
"""


class App:
    """サーバー全体で共有する状態（保存データ・調査・ごみ箱への移動）。"""

    def __init__(self, db: Store, backend=None) -> None:
        self.db = db
        self.backend = backend  # テストでは偽の移動処理を渡す（None ならWindows のごみ箱）
        self.token = secrets.token_urlsafe(24)
        self.scan_thread: threading.Thread | None = None
        self.scan_ctl: autoscan.Controller | None = None
        self.scan_progress: dict = {}
        self.scan_result: dict | None = None
        self.trash_job: dict | None = None
        self._lock = threading.Lock()
        self._thumb_lock = threading.Semaphore(2)
        self._picker_lock = threading.Lock()
        self._audio_cache: dict = {}
        self.port = DEFAULT_PORT
        self.autostart_registry = None  # テストでは偽の登録先を渡す（None なら Windows のレジストリ）
        self.shutdown = None            # サーバーを止める処理（main() が設定する）

    # --- 共通 -----------------------------------------------------------------
    def tools(self) -> media.FFmpegTools:
        return media.find_ffmpeg(self.db.get_setting("ffmpeg_dir"))

    def ctx(self) -> candidates.Context:
        return candidates.Context(self.db)

    def row_json(self, r: dict, ctx: candidates.Context) -> dict:
        meta = candidates.loads(r["meta"], {}) or {} if r.get("meta") else {}
        return {"id": r["id"], "name": r["name"], "path": r["path"], "folder": r["folder"], "size": r["size"],
                "mtime": r["mtime"], "taken_at": r["taken_at"], "ext": r["ext"] or "",
                "duration": candidates.duration(r), "width": meta.get("width"), "height": meta.get("height"),
                "cloud_only": bool(r["cloud_only"]), "protected": bool(r["protected"]),
                "favorite": bool(r.get("favorite")), "decision": r["decision"], "missing": bool(r.get("missing")),
                "reasons": ctx.short_reasons(r), "detail": ctx.reasons(r)}

    def videos(self, ctx: candidates.Context) -> list[dict]:
        return [r for r in ctx.rows.values() if r["kind"] == "video" and not r.get("missing")]

    # --- 状態 -----------------------------------------------------------------
    def state(self) -> dict:
        ctx = self.ctx()
        vids = self.videos(ctx)
        plan = candidates.plan_recordings(ctx, None)
        chosen = [r for r in vids if r["decision"] == "削除候補" and not r["protected"]]
        tools = self.tools()
        paths = [loc["path"] for loc in self.db.locations()] or [os.environ.get("SystemDrive", "C:") + "\\"]
        last = self.db.get_setting("scan_finished")
        return {
            "app": APP_NAME, "version": __version__,
            "videos": len(vids), "video_bytes": sum(r["size"] for r in vids),
            "plan_count": len(plan["items"]), "plan_bytes": plan["total"],
            "duplicate_bytes": plan["duplicate_total"],
            "chosen_count": len(chosen), "chosen_bytes": sum(r["size"] for r in chosen),
            "drives": candidates.drive_space(paths),
            "ffmpeg": tools.available, "ffmpeg_source": tools.source,
            "scanning": self.scan_thread is not None and self.scan_thread.is_alive(),
            "progress": self.scan_progress, "scan_result": self.scan_result,
            "analysis_state": self.db.get_setting("analysis_state"),
            "last_scan": float(last) if last else None,
            "protection_ok": self.db.protection_ok, "problems": list(self.db.load_problems),
            "trashing": bool(self.trash_job and not self.trash_job.get("done")),
            "not_scanned": self.db.json_list("not_scanned_drives"),
            "data_dir": str(self.db.data_dir),
            "scan_folders": self.db.json_list("scan_folders"),
            "skipped_errors": self.db.skip_count("error"),
            "locations": [{"path": x["path"], "label": x.get("label"), "status": x.get("status"),
                           "files": x.get("files", 0)} for x in self.db.locations()],
        }

    # --- 調査 -----------------------------------------------------------------
    def set_scan_folders(self, folders: list) -> dict:
        """調べるフォルダを保存する（空なら PC 全体を調べる既定の動きに戻す）。存在しない・読めないものは保存しない。"""
        checked = []
        for raw in folders:
            path, err = check_folder(raw)
            if err:
                return {"ok": False, "error": err}
            checked.append(path)
        if self.scan_thread is not None and self.scan_thread.is_alive():
            return {"ok": False, "error": "調査中は調べるフォルダを変更できません。中断してからもう一度操作してください"}
        self.db.set_json_list("scan_folders", checked)
        return {"ok": True, "scan_folders": self.db.json_list("scan_folders")}

    def scan_problem(self) -> str | None:
        """調べるフォルダが今も使えるか（外付けドライブを外した・名前を変えた、など）。"""
        for path in self.db.json_list("scan_folders"):
            _, err = check_folder(path)
            if err:
                return err + "（調べるフォルダを指定し直してください）"
        return None

    def start_scan(self) -> dict:
        with self._lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                # 再読み込み・連打で同じ調査を二重に始めない（今の調査の進み具合をそのまま表示する）
                return {"ok": True, "already_running": True}
            problem = self.scan_problem()
            if problem:  # 調べられない場所を「調べた結果 0 本」として扱わない
                self.scan_result = {"error": problem}
                return {"ok": False, "error": problem}
            self.scan_ctl = ctl = autoscan.Controller()
            self.scan_progress = {"text": "調査を始めています"}
            self.scan_result = None
            tools = self.tools()

            def run():
                try:
                    self.scan_result = autoscan.task_autoscan(self.db, tools, ctl, self._report)
                except Exception as e:  # noqa: BLE001  画面を「調査中」のままにしない
                    self.scan_result = {"error": str(e) or type(e).__name__}
                finally:
                    self.scan_progress = {}
            self.scan_thread = threading.Thread(target=run, name="recsort-scan", daemon=True)
            self.scan_thread.start()
        return {"ok": True}

    def _report(self, info: dict) -> None:
        self.scan_progress = {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool)) or v is None}

    def cancel_scan(self) -> dict:
        if self.scan_ctl:
            self.scan_ctl.cancel()
        return {"ok": True}

    # --- 一覧・候補 -------------------------------------------------------------
    def files(self) -> dict:
        ctx = self.ctx()
        return {"items": [self.row_json(r, ctx) for r in self.videos(ctx)]}

    def plan(self, target_gb: float | None, drive: str | None) -> dict:
        ctx = self.ctx()
        target = int(target_gb * 1024 ** 3) if target_gb else None
        plan = candidates.plan_recordings(ctx, target, drive=drive or None)
        rows = {}
        for it in plan["items"]:
            for mid in (it["id"], it.get("keep_id")):
                if mid is not None and mid in ctx.rows:
                    rows[mid] = self.row_json(ctx.rows[mid], ctx)
        groups = []
        for n, members in enumerate(ctx.video_groups):
            groups.append({"index": n, "members": members,
                           "confirmed": candidates.similar_keep_confirmed(ctx, n)})
        plan["rows"] = rows
        plan["similar_groups"] = groups
        plan["drives"] = sorted({candidates.drive_of(r["path"]) for r in self.videos(ctx)})
        if plan["target"] == float("inf"):
            plan["target"] = None
        return plan

    def group(self, mid: int) -> dict:
        """比較の対象: 完全重複のグループ、または類似録画のグループ。"""
        ctx = self.ctx()
        if mid in ctx.dup_of:
            kind, members = "duplicate", ctx.dup_groups[ctx.dup_of[mid]]
            keep = ctx.dup_keep[ctx.dup_of[mid]]
        elif mid in ctx.vgroup_of:
            n = ctx.vgroup_of[mid]
            kind, members = "similar", ctx.video_groups[n]
            confirmed = candidates.similar_keep_confirmed(ctx, n)
            keep = (confirmed, "比較して残す1本に選んだ録画") if confirmed else ctx.vgroup_keep[n]
        else:
            kind, members, keep = "single", [mid], (mid, "")
        members = [m for m in members if m in ctx.rows]
        result = {"kind": kind, "keep_id": keep[0], "keep_reason": keep[1],
                  "items": [self.row_json(ctx.rows[m], ctx) for m in members]}
        if kind == "similar":
            result.update(self.scene_table(ctx, members, keep[0]))
        return result

    def scene_table(self, ctx, members: list[int], keep_id) -> dict:
        """似た録画の場面ごとの比較（基準の1本と、ほかの録画の同じ位置の場面が一致したか）。

        比較画面で「違う場面」を一覧し、その位置へ飛んで両方を見比べられるようにする。
        """
        ref = keep_id if keep_id in members else members[0]
        ref_row = ctx.rows[ref]
        dur = candidates.duration(ref_row) or 0
        n = similar.N_POINTS
        scenes = [{"index": i, "time": round(dur * (i + 0.5) / n, 2), "status": {str(ref): "ref"}, "corr": {}}
                  for i in range(n)]
        differ = set()
        for m in members:
            if m == ref:
                continue
            d = candidates.similar_pair_detail(ref_row, ctx.rows[m])
            ok = d.get("per_ok") or [None] * n
            for i in range(n):
                v = ok[i] if i < len(ok) else None
                scenes[i]["status"][str(m)] = "match" if v is True else "differ" if v is False else "unknown"
                if i < len(d["per_frame"]) and d["per_frame"][i] is not None:
                    scenes[i]["corr"][str(m)] = d["per_frame"][i]
                if v is False:
                    differ.add(i)
        has_audio = {str(m): bool((candidates.loads(ctx.rows[m]["meta"], {}) or {}).get("has_audio"))
                     for m in members}
        return {"reference_id": ref, "scenes": scenes, "differ_scenes": sorted(differ), "has_audio": has_audio,
                "audio_note": "音声は自動では比べていません。「音声を比べる」で一部の区間を比べるか、"
                              "両方を再生して聞き比べてください"}

    def audio_check(self, a: int, b: int) -> dict:
        """2本の音声を数区間だけ比べる（比較画面から押したときだけ。結果はこの起動中だけ覚える）。"""
        ra, rb = self.db.video(a), self.db.video(b)
        if not ra or not rb or ra["kind"] != "video" or rb["kind"] != "video":
            return {"ok": False, "error": "録画が見つかりません"}
        if ra["missing"] or rb["missing"] or ra["cloud_only"] or rb["cloud_only"]:
            return {"ok": True, "verdict": "unknown", "reason": "オンライン専用・見つからない録画は音声を比べません",
                    "windows": [], "matched": 0, "compared": 0}
        key = (min(a, b), max(a, b), ra["size"], ra["mtime"], rb["size"], rb["mtime"])
        cached = self._audio_cache.get(key)
        if cached is None:
            dur = min(candidates.duration(ra) or 0, candidates.duration(rb) or 0)
            if dur <= 0:
                info = media.probe(self.tools(), ra["path"]) if self.tools().available else {}
                dur = info.get("duration") or 0
            cached = similar.audio_compare(self.tools(), ra["path"], rb["path"], dur)
            self._audio_cache[key] = cached
        return {"ok": True, **cached}

    def scene_image(self, mid: int, index: int) -> str | None:
        """比較画面の場面の画像（代表場面と同じ位置）。録画は読み取るだけ。作った画像はキャッシュに置く。"""
        row = self.db.video(mid)
        if row is None or row["cloud_only"] or row["missing"] or row["kind"] != "video":
            return None
        if not 0 <= index < similar.N_POINTS:
            return None
        out = str(self.db.thumb_dir / f"s{mid}_{int(row['mtime'])}_{row['size']}_{index}.jpg")
        if os.path.exists(out):
            return out
        tools = self.tools()
        dur = candidates.duration(row) or 0
        if not tools.available or dur <= 0:
            return None
        t = dur * (index + 0.5) / similar.N_POINTS
        with self._thumb_lock:
            tmp = out[:-4] + ".tmp.jpg"
            cp = media._run([tools.ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", row["path"], "-frames:v", "1",
                             "-vf", "scale=240:-2", "-q:v", "5", tmp], None, timeout=60)
            if cp.returncode != 0 or not os.path.exists(tmp):
                return None
            os.replace(tmp, out)
        return out

    # --- 判断 -----------------------------------------------------------------
    def decide(self, ids: list[int], decision) -> dict:
        if decision not in DECISIONS:
            return {"ok": False, "error": "判断の値が正しくありません"}
        if decision == "削除候補":  # 保護中は削除候補にしない（デスクトップ版と同じ）
            ids = [m for m in ids if self.db.video(m) and not self.db.video(m)["protected"]]
        skipped = self.db.set_decisions(ids, decision)  # 戻り値は記録しなかった（保護中の）ID
        return {"ok": True, "skipped": len(skipped)}

    def keep_one(self, keep: int, others: list[int]) -> dict:
        """この1本を残す（まだ移動しない）。

        完全に同じ録画（中身が一致）のときだけ、同じグループのほかのコピーを削除候補にする。
        似た録画は同じ内容とは限らないので、ほかの録画は変えない（削除候補にするときは録画ごとに選ぶ）。
        保護中は削除候補にしない。グループ外の ID が送られてきても無視する。
        """
        ctx = self.ctx()
        if keep not in ctx.rows:
            return {"ok": False, "error": "録画が見つかりません"}
        if keep in ctx.dup_of:
            members = set(ctx.dup_groups[ctx.dup_of[keep]])
            targets = [m for m in others if m != keep and m in members
                       and self.db.video(m) and not self.db.video(m)["protected"]]
            kind = "duplicate"
        else:
            targets = []
            kind = "similar" if keep in ctx.vgroup_of else "single"
        self.db.set_decision(keep, "残す")
        if targets:
            self.db.set_decisions(targets, "削除候補")
        return {"ok": True, "count": len(targets), "kind": kind}

    # --- 削除候補・ごみ箱 ---------------------------------------------------------
    def chosen(self) -> dict:
        ctx = self.ctx()
        items = []
        for r in self.videos(ctx):
            if r["decision"] != "削除候補":
                continue
            item = self.row_json(r, ctx)
            item["blocked"] = trash.check(self.db, r["id"], ctx)
            keep = ctx.dup_keep.get(ctx.dup_of.get(r["id"])) if r["id"] in ctx.dup_of else None
            item["keeper"] = ctx.rows[keep[0]]["path"] if keep and keep[0] != r["id"] else None
            items.append(item)
        return {"items": items, "generation": self.db.generation}

    def start_trash(self, ids: list[int], generation: int | None) -> dict:
        with self._lock:
            if self.trash_job and not self.trash_job.get("done"):
                return {"ok": False, "error": "ごみ箱への移動を実行中です"}
            job = {"done": False, "current": 0, "total": len(ids), "name": "", "results": [],
                   "cancel": threading.Event(), "error": None}
            self.trash_job = job

        def progress(n, total, name):
            job.update(current=n + 1, total=total, name=name)

        def run():
            try:
                with trash.windows_com_thread():  # Windows のごみ箱 API は、呼び出すスレッドで COM の初期化が必要
                    results = trash.move_to_trash(self.db, ids, self.backend, progress=progress,
                                                  cancel=job["cancel"], expected_generation=generation)
                job["results"] = [{"id": r.media_id, "path": r.path, "size": r.size, "status": r.status,
                                   "message": r.message, "record_error": r.record_error} for r in results]
            except Exception as e:  # noqa: BLE001  画面を「実行中」のままにしない
                job["error"] = str(e) or type(e).__name__
            finally:
                job["done"] = True
        threading.Thread(target=run, name="recsort-trash", daemon=True).start()
        return {"ok": True}

    def trash_status(self) -> dict:
        job = self.trash_job
        if not job:
            return {"running": False}
        return {"running": not job["done"], **{k: v for k, v in job.items() if k != "cancel"}}

    # --- 設定 -----------------------------------------------------------------
    def pick_folder(self) -> dict:
        """このパソコンでフォルダを選ぶ画面を開く（ブラウザはフォルダのフルパスを教えてくれないため）。"""
        if not self._picker_lock.acquire(blocking=False):
            return {"ok": False, "error": "フォルダを選ぶ画面がすでに開いています（タスクバーを確認してください）"}
        try:
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # 黒い窓を出さない
            done = subprocess.run([sys.executable, "-c", _PICKER], capture_output=True, text=True,
                                  timeout=900, **kw)
            if done.returncode != 0:
                raise RuntimeError(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "起動できません")
            path = json.loads(done.stdout.strip().splitlines()[-1])
        except Exception as e:  # noqa: BLE001  tkinter がない Python など。パスの入力で続けられる
            return {"ok": False, "error": "フォルダを選ぶ画面を開けませんでした。パスを直接入力してください"
                                          f"（{e}）"}
        finally:
            self._picker_lock.release()
        return {"ok": True, "path": os.path.normpath(path) if path else ""}

    def autostart_status(self) -> dict:
        from . import autostart
        try:
            return {"ok": True, **autostart.status(self.autostart_registry, self._autostart_port())}
        except OSError as e:
            return {"ok": False, "error": f"自動起動の設定を読み取れませんでした: {e}"}

    def set_autostart(self, enabled: bool) -> dict:
        """利用者が設定画面で選んだときだけ登録・解除する。"""
        from . import autostart
        try:
            fn = autostart.enable if enabled else autostart.disable
            return {"ok": True, **fn(self.autostart_registry, self._autostart_port())}
        except autostart.Unsupported as e:
            return {"ok": False, "error": str(e)}
        except OSError as e:
            return {"ok": False, "error": f"自動起動を{'登録' if enabled else '解除'}できませんでした: {e}"}

    def _autostart_port(self) -> int | None:
        return None if self.port == DEFAULT_PORT else self.port

    def settings(self) -> dict:
        tools = self.tools()
        return {"ffmpeg_dir": self.db.get_setting("ffmpeg_dir") or "", "ffmpeg": tools.available,
                "scan_folders": self.db.json_list("scan_folders"),
                "ffmpeg_source": tools.source, "auto_scan": self.db.get_setting("auto_scan_on_start", "1") != "0",
                "locations": self.db.locations(), "extra_locations": self.db.json_list("extra_locations"),
                "excluded_folders": self.db.json_list("excluded_folders"),
                "not_scanned": self.db.json_list("not_scanned_drives"), "data_dir": str(self.db.data_dir)}

    def save_settings(self, data: dict) -> dict:
        if "ffmpeg_dir" in data:
            self.db.set_setting("ffmpeg_dir", str(data["ffmpeg_dir"]).strip() or None)
        if "auto_scan" in data:
            self.db.set_setting("auto_scan_on_start", "1" if data["auto_scan"] else "0")
        for key in ("extra_locations", "excluded_folders"):
            if key in data:
                values = [str(v).strip() for v in data[key] if str(v).strip()]
                bad = [v for v in values if not os.path.isabs(v)]
                if bad:
                    return {"ok": False, "error": f"場所はフルパスで指定してください: {bad[0]}"}
                self.db.set_json_list(key, values)
        return {"ok": True, **self.settings()}

    # --- 録画の再生・サムネイル -----------------------------------------------------
    def media_path(self, mid: int) -> str | None:
        row = self.db.video(mid)
        if row is None or row["missing"] or row["cloud_only"] or row["kind"] != "video":
            return None  # 登録した録画だけを返す（任意のファイルは読ませない）。オンライン専用は勝手にダウンロードしない
        return row["path"]

    def thumbnail(self, mid: int) -> str | None:
        row = self.db.video(mid)
        if row is None or row["cloud_only"] or row["missing"]:
            return None
        out = str(self.db.thumb_dir / f"m{mid}_{int(row['mtime'])}_{row['size']}.jpg")
        if os.path.exists(out):
            return out
        tools = self.tools()
        if not tools.available:
            return None
        with self._thumb_lock:  # 同時に作るのは2本まで（ほかの操作を重くしない）
            if os.path.exists(out):
                return out
            try:
                info = media.probe(tools, row["path"])
                paths = media.make_thumbnails(tools, row["path"], info.get("duration", 0), out[:-4], None)
            except Exception:  # noqa: BLE001  作れなければ「プレビューなし」と表示する
                return None
            if not paths:
                return None
            os.replace(paths[min(1, len(paths) - 1)], out)
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)  # アプリのキャッシュ内の一時画像だけを消す
        return out

    def reveal(self, mid: int) -> dict:
        row = self.db.video(mid)
        if row is None:
            return {"ok": False, "error": "見つかりません"}
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(row["path"])])
            return {"ok": True}
        return {"ok": False, "error": "この操作は Windows でだけ使えます"}


class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    server_version = "RecSort"

    def log_message(self, fmt, *args):  # 録画のパスをコンソールに出さない
        pass

    # --- 応答 -----------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    # --- 安全確認 ---------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-RecSort-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given, self.app.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            raise ValueError("要求が大きすぎます")
        raw = self.rfile.read(n) if n > 0 else b""
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("要求の形式が正しくありません")
        return data

    # --- 振り分け ---------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "このアドレスからは開けません")
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path
        if path == HEALTH_PATH:  # 起動確認用（合言葉なし）。録画の情報は返さない
            return self._json({"app": APP_NAME, "service": "recsort", "version": __version__})
        if not path.startswith("/api/"):
            return self._static(path)
        if not self._token_ok(q):
            return self._error(HTTPStatus.FORBIDDEN, "ページを開き直してください（合言葉が一致しません）")
        try:
            if path == "/api/state":
                return self._json(self.app.state())
            if path == "/api/files":
                return self._json(self.app.files())
            if path == "/api/plan":
                target = float(q["target"][0]) if q.get("target") and q["target"][0] else None
                return self._json(self.app.plan(target, (q.get("drive") or [None])[0]))
            if path.startswith("/api/group/"):
                return self._json(self.app.group(int(path.rsplit("/", 1)[1])))
            if path == "/api/chosen":
                return self._json(self.app.chosen())
            if path == "/api/trash":
                return self._json(self.app.trash_status())
            if path == "/api/settings":
                return self._json(self.app.settings())
            if path == "/api/autostart":
                return self._json(self.app.autostart_status())
            if path.startswith("/api/media/"):
                return self._file(self.app.media_path(int(path.rsplit("/", 1)[1])))
            if path.startswith("/api/scene/"):
                _, _, _, mid, idx = path.split("/")
                return self._file(self.app.scene_image(int(mid), int(idx)), "image/jpeg")
            if path.startswith("/api/audio/"):
                _, _, _, a, b = path.split("/")
                return self._json(self.app.audio_check(int(a), int(b)))
            if path.startswith("/api/thumb/"):
                return self._file(self.app.thumbnail(int(path.rsplit("/", 1)[1])), "image/jpeg")
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "要求の形式が正しくありません")
        return self._error(HTTPStatus.NOT_FOUND, "見つかりません")

    def do_POST(self):
        # 断る場合も、送られてきた中身を先に読み切る（読まずに応答すると Windows では接続が切れて見える）
        try:
            data = self._body()
        except ValueError:
            data = None
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "このアドレスからは開けません")
        url = urlparse(self.path)
        if not self._token_ok({}):  # 書き込みはヘッダーの合言葉だけを受け付ける（URL に載せない）
            return self._error(HTTPStatus.FORBIDDEN, "ページを開き直してください（合言葉が一致しません）")
        try:
            if data is None:
                raise ValueError("要求の形式が正しくありません")
            ids = [int(i) for i in data.get("ids", [])]
            p = url.path
            if p == "/api/scan":
                if "folders" in data:  # 調べるフォルダを指定して調べる（空のリストは PC 全体）
                    if not isinstance(data["folders"], list):
                        raise ValueError("folders")
                    saved = self.app.set_scan_folders(data["folders"])
                    if not saved["ok"]:
                        return self._json(saved)
                return self._json(self.app.start_scan())
            if p == "/api/folders":
                if not isinstance(data.get("folders"), list):
                    raise ValueError("folders")
                return self._json(self.app.set_scan_folders(data["folders"]))
            if p == "/api/folder/check":
                path, err = check_folder(data.get("path"))
                return self._json({"ok": err is None, "path": path, "error": err})
            if p == "/api/folder/pick":
                return self._json(self.app.pick_folder())
            if p == "/api/scan/cancel":
                return self._json(self.app.cancel_scan())
            if p == "/api/decide":
                return self._json(self.app.decide(ids, data.get("decision")))
            if p == "/api/keep":
                return self._json(self.app.keep_one(int(data["keep"]), ids))
            if p == "/api/protect":
                self.app.db.set_protected(int(data["id"]), bool(data["value"]))
                return self._json({"ok": True})
            if p == "/api/favorite":
                self.app.db.set_favorite(int(data["id"]), bool(data["value"]))
                return self._json({"ok": True})
            if p == "/api/viewed":
                self.app.db.mark_viewed(int(data["id"]))
                return self._json({"ok": True})
            if p == "/api/trash":
                if data.get("confirm") is not True:  # 最終確認の承認がない要求では移動しない
                    return self._error(HTTPStatus.BAD_REQUEST, "最終確認がありません")
                gen = data.get("generation")
                return self._json(self.app.start_trash(ids, int(gen) if gen is not None else None))
            if p == "/api/trash/cancel":
                job = self.app.trash_job
                if job:
                    job["cancel"].set()
                return self._json({"ok": True})
            if p == "/api/settings":
                return self._json(self.app.save_settings(data))
            if p == "/api/autostart":
                if not isinstance(data.get("enabled"), bool):
                    raise ValueError("enabled")
                return self._json(self.app.set_autostart(data["enabled"]))
            if p == "/api/shutdown":
                if data.get("confirm") is not True:
                    return self._error(HTTPStatus.BAD_REQUEST, "確認がありません")
                if self.app.trash_job and not self.app.trash_job.get("done"):
                    return self._json({"ok": False, "error": "ごみ箱へ移動している間は終了できません"})
                if not self.app.shutdown:
                    return self._json({"ok": False, "error": "この起動方法では画面から終了できません"})
                self.app.shutdown()
                return self._json({"ok": True})
            if p == "/api/reveal":
                return self._json(self.app.reveal(int(data["id"])))
        except StoreError as e:  # 保存できなかったことを、成功したように見せずに伝える
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存できませんでした: {e}")
        except (ValueError, KeyError, TypeError):
            return self._error(HTTPStatus.BAD_REQUEST, "要求の形式が正しくありません")
        return self._error(HTTPStatus.NOT_FOUND, "見つかりません")

    # --- ファイル -------------------------------------------------------------
    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else unquote(path.lstrip("/"))
        target = (UI_DIR / name).resolve()
        if UI_DIR not in target.parents or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "見つかりません")
        body = target.read_bytes()
        if name == "index.html":  # 合言葉は、このパソコンで開いたページにだけ渡す
            body = body.replace(b"__RECSORT_TOKEN__", self.app.token.encode())
        ctype = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, body, ctype, {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:"})

    def _file(self, path: str | None, ctype: str | None = None) -> None:
        """録画・サムネイルを返す。動画のシークのため Range（一部分だけ）にも対応する。読み取りだけ。"""
        if not path or not os.path.isfile(path):
            return self._error(HTTPStatus.NOT_FOUND, "見つかりません")
        size = os.path.getsize(path)
        ctype = (ctype or VIDEO_TYPES.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0]
                 or "application/octet-stream")
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:
                    start, end = max(size - int(b), 0), size - 1
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass  # 再生位置を変えるとブラウザが途中で接続を切る（正常）


class LocalServer(ThreadingHTTPServer):
    """127.0.0.1 だけで待ち受けるサーバー。

    Windows の SO_REUSEADDR は「ほかのアプリが使っている番号にも割り込める」意味になるため使わず、
    SO_EXCLUSIVEADDRUSE で、使われている番号では確実に起動に失敗するようにする（別のアプリと取り合わない）。
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return LocalServer(("127.0.0.1", port), handler)


def probe(port: int, timeout: float = 2.0) -> str:
    """その番号で何が動いているか。"recsort" / "other"（別のアプリ）/ "none"（何も待ち受けていない）。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", HEALTH_PATH, headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read(4096)
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            return "other"
        return "recsort" if res.status == 200 and isinstance(data, dict) and data.get("service") == "recsort" \
            else "other"
    except ConnectionRefusedError:
        return "none"
    except OSError:  # 接続できたが HTTP で答えない・時間切れ など
        return "other"
    finally:
        conn.close()


def wait_ready(port: int, timeout: float = 30.0) -> bool:
    """サーバーが応答できる状態になるまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(port, 1.0) == "recsort":
            return True
        time.sleep(0.2)
    return False


def _port_from(argv: list[str]) -> int:
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            return int(argv[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    return int(os.environ.get("RECSORT_PORT") or DEFAULT_PORT)


def _open_browser(url: str) -> None:
    try:
        if not webbrowser.open(url):
            print(f"ブラウザを開けませんでした。次のアドレスをブラウザで開いてください: {url}")
    except Exception:  # noqa: BLE001
        print(f"ブラウザを開けませんでした。次のアドレスをブラウザで開いてください: {url}")


def _stop_on_signals() -> None:
    """ウィンドウを閉じた（Windows: SIGBREAK）・終了の要求（SIGTERM）でも、Ctrl+C と同じく記録を保存して終わる。"""
    import signal

    def stop(*_):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (OSError, ValueError):
                pass


def notify_failure(lines: list[str], background: bool) -> None:
    """起動できなかった理由と直し方を伝える。自動起動（黒い窓なし）のときは Windows の通知ダイアログでも示す。"""
    for line in lines:
        print(line)
    if background and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "\n".join(lines), "RecSort を起動できませんでした", 0x30 | 0x40000)
        except Exception:  # noqa: BLE001  ダイアログを出せなくても、記録（server.log）には残る
            pass


def open_log(data_dir: Path):
    """自動起動（黒い窓なし）のときの記録先。大きくなったら1世代だけ残して作り直す。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    log = data_dir / "server.log"
    try:
        if log.exists() and log.stat().st_size > 1024 * 1024:
            os.replace(log, data_dir / "server.log.1")
    except OSError:
        pass
    fh = open(log, "a", encoding="utf-8", buffering=1)
    fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 起動 ---\n")
    return fh


def main(argv: list[str] | None = None) -> int:
    from .instance_lock import AlreadyRunning, InstanceLock
    from .store import default_data_dir
    argv = sys.argv[1:] if argv is None else argv
    background = "--background" in argv  # ログイン時の自動起動: サーバーだけ（ブラウザを開かない・調査しない）
    open_browser = "--no-browser" not in argv and not background
    data_dir = default_data_dir()
    if background:
        try:
            sys.stdout = sys.stderr = open_log(data_dir)
        except OSError:
            pass
    try:
        port = _port_from(argv)
    except ValueError:
        notify_failure(["--port には番号（例: 8765）を指定してください。"], background)
        return 2
    url = f"http://127.0.0.1:{port}/"
    log_hint = f"詳しい記録: {data_dir / 'server.log'}" if background else ""

    # 1. すでに RecSort が動いていれば、その画面を開くだけにする（2つ目は起動しない）
    found = probe(port)
    if found == "recsort":
        print(f"RecSort はすでに起動しています。開いている画面を表示します: {url}" if open_browser else
              f"RecSort はすでに起動しています（2つ目は起動しません）: {url}")
        if open_browser:
            _open_browser(url)
        return 0
    if found == "other":
        notify_failure([f"ポート {port} は別のアプリが使用しているため、RecSort を起動できません。",
                        "そのアプリを終了してから RecSortを起動.bat をダブルクリックしてください。",
                        "または別の番号で起動してください（例: RecSortを起動.bat --port 8766）。", log_hint], background)
        return 3

    try:  # 保存データを読み書きする前に、二重起動を防ぐロックを取る（デスクトップ版とも共通）
        lock = InstanceLock(data_dir).acquire()
    except AlreadyRunning:
        if wait_ready(port, 10):  # ほぼ同時にダブルクリックした場合: 先に起動した方の画面を開く
            print(f"RecSort はすでに起動しています。開いている画面を表示します: {url}")
            if open_browser:
                _open_browser(url)
            return 0
        notify_failure(["RecSort はすでに起動しています（デスクトップ版、または別の番号のブラウザ版）。",
                        "保護や判断の記録が食い違わないよう、同時に2つは起動できません。",
                        "起動中の RecSort を終了してから、もう一度起動してください。", log_hint], background)
        return 1
    except OSError as e:
        notify_failure([f"保存先（{data_dir}）を使えないため起動できません: {e}", log_hint], background)
        return 1
    try:
        try:
            db = Store(data_dir)
        except Exception as e:  # noqa: BLE001
            notify_failure([f"保存データを読み込めませんでした（{data_dir}）: {e}",
                            "保存先のファイルを確認してください（壊れたファイルは .broken-* に退避されます）。", log_hint],
                           background)
            return 1
        app = App(db)
        app.port = port
        try:
            server = make_server(app, port)
        except OSError as e:
            notify_failure([f"ポート {port} を使えないため、RecSort を起動できません（{e.strerror or e}）。",
                            "別のアプリが使用している可能性があります。そのアプリを終了するか、--port で別の番号を指定してください。",
                            log_hint], background)
            return 3
        def stop_later():  # 「終了しました」の応答を返してから止める
            time.sleep(0.3)
            server.shutdown()
        app.shutdown = lambda: threading.Thread(target=stop_later, daemon=True).start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/"
        print(f"RecSort {__version__} を起動しました: {url}" + ("（ログイン時の自動起動）" if background else ""))
        print(f"保存データ: {data_dir}")
        if not background:
            print("このアドレスをブラウザで開いても使えます。このウィンドウを閉じるか Ctrl+C で終了します。")

        def open_when_ready():  # 応答できる状態になってからブラウザを開く
            if wait_ready(port):
                if open_browser:
                    _open_browser(url)
            else:
                print("サーバーの応答を確認できませんでした。しばらくしてから上のアドレスを開いてください。")
        threading.Thread(target=open_when_ready, name="recsort-open", daemon=True).start()
        tools = app.tools()
        if not tools.available:
            print("FFmpeg が見つかりません（長さ・黒画面・無音・類似の判定とサムネイルが使えません）。"
                  "例: winget install Gyan.FFmpeg")
        if db.get_setting("auto_scan_on_start", "1") != "0" and "--no-scan" not in argv and not background:
            started = app.start_scan()
            if not started.get("ok"):
                print("自動調査を始めませんでした: " + str(started.get("error")))
        _stop_on_signals()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if app.scan_ctl:
                app.scan_ctl.cancel()
            if app.scan_thread:
                app.scan_thread.join(15)
            db.flush_user_state()
            db.save_scan_state()
            print("RecSort を終了しました。")
        return 0
    finally:
        lock.release()
