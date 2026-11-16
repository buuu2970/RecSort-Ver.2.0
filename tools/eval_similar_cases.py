"""類似判定の変更前後の比較（合成動画だけを使う。実際の録画は使わない）。

使い方: python tools/eval_similar_cases.py [作業フォルダ] [--json 結果.json]

同じ代表場面（シグネチャ）に対して「変更前の基準」と「今の基準」の両方で判定し、次を記録する:
- 再圧縮・解像度違い（9 素材 × 3 組 = 27 組）: 見つけた数（多いほど良い）
- 明暗差が小さい映像（低コントラスト 4 素材 × 3 組 = 12 組）: 見つけた数
- 共通画面が長い別動画（20 秒のうち 16 秒が共通）・一部の場面だけ違う動画: 類似にしてしまった数（少ないほど良い）
- 音声だけ違う動画: 映像は同じなので類似に入る。音声の比較（audio_compare）で違いを示せるか
- 別の動画どうし（低コントラスト・暗いノイズの別動画を含む）: 誤ってまとめた数
- 比較・グループ化の時間（代表場面の取り出しは両基準で同じなので別に測る）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage_organizer import media, similar  # noqa: E402

S = "s=320x180:r=15"
REENCODE = {"A": f"testsrc2={S}", "B": f"mandelbrot={S}", "C": f"life={S}:mold=10",
            "D": f"cellauto={S}:rule=110", "E": f"gradients={S}:speed=0.05", "F": f"sierpinski={S}",
            "G": f"testsrc={S},rotate=a=0.2*t", "H": f"mandelbrot={S}:start_x=-0.75:start_y=0.1:end_scale=0.1",
            "I": f"sierpinski={S}:type=triangle"}
# 明暗差が小さい映像（夜の場面・霧・暗いゲーム画面を想定）。同じ素材の再圧縮・縮小は見つけたい
LOW = {"L1": f"testsrc2={S},eq=contrast=0.08:brightness=-0.35",
       "L2": f"mandelbrot={S},eq=contrast=0.06",
       "L3": f"gradients={S}:speed=0.02:c0=0x303030:c1=0x383838:c2=0x343434:c3=0x2c2c2c:n=4",
       "L4": f"life={S}:mold=10,eq=contrast=0.1:brightness=-0.3"}
# 低コントラスト素材のうち、再圧縮用の素材と同じ映像を暗く・明暗差を小さくしたもの（L1 = A、L2 = B）。
# 明るさ・コントラストだけが違う同じ映像なので、類似として見つけてよい（別の動画として数えない）
SAME_FOOTAGE = {"L1": "A", "L2": "B"}
# 別の動画どうし（誤ってまとめてはいけない）: 暗いノイズだけの映像・別の低コントラスト映像
NEGATIVE = {"N1": f"nullsrc={S},geq=random(1)*12+20:128:128",
            "N2": f"nullsrc={S},geq=random(1)*12+20:128:128,hue=s=0",
            "N3": f"gradients={S}:speed=0.03:c0=0x303030:c1=0x343434:seed=7",
            "N4": f"testsrc={S},eq=contrast=0.07:brightness=-0.35"}
SECONDS = 20


def ff(tools, *args):
    subprocess.run([tools.ffmpeg, "-v", "error", "-y", *args], check=True)


def variants(tools, root: Path, key: str, src: str) -> list[str]:
    orig = root / f"{key}_orig.mp4"
    ff(tools, "-f", "lavfi", "-i", src, "-t", str(SECONDS), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20",
       str(orig))
    ff(tools, "-i", str(orig), "-vf", "scale=160:90", "-c:v", "libx264", "-crf", "28", str(root / f"{key}_lowres.mp4"))
    ff(tools, "-i", str(orig), "-c:v", "libx264", "-crf", "40", str(root / f"{key}_recompress.mp4"))
    return [f"{key}_orig", f"{key}_lowres", f"{key}_recompress"]


def concat(tools, root: Path, name: str, parts: list[Path]) -> str:
    lst = root / f"_{name}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    ff(tools, "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-pix_fmt", "yuv420p",
       str(root / f"{name}.mp4"))
    return name


def clip(tools, root: Path, name: str, src: str, seconds: float, *extra) -> Path:
    p = root / f"_{name}.mp4"
    ff(tools, "-f", "lavfi", "-i", src, "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264", *extra, str(p))
    return p


def make_samples(tools, root: Path) -> dict:
    groups = {"reencode": [], "low": [], "negative": [], "common": [], "partial": [], "audio": []}
    for key, src in REENCODE.items():
        groups["reencode"].append(variants(tools, root, key, src))
    for key, src in LOW.items():
        groups["low"].append(variants(tools, root, key, src))
    for key, src in NEGATIVE.items():
        ff(tools, "-f", "lavfi", "-i", src, "-t", str(SECONDS), "-pix_fmt", "yuv420p", "-c:v", "libx264",
           str(root / f"{key}_orig.mp4"))
        groups["negative"].append(f"{key}_orig")
    # 共通画面が長い別動画: 共通の始め 8 秒 + 本編 4 秒（別々）+ 共通の終わり 8 秒 = 20 秒のうち 16 秒が共通
    head = clip(tools, root, "head", f"smptehdbars={S}", 8)
    tail = clip(tools, root, "tail", f"testsrc={S}", 8)
    pairs = []
    for n, (x, y) in enumerate([(f"life={S}:mold=10", f"cellauto={S}:rule=110"),
                                (f"mandelbrot={S}", f"sierpinski={S}"),
                                (f"testsrc2={S}", f"cellauto={S}:rule=30")]):
        bx, by = clip(tools, root, f"bx{n}", x, 4), clip(tools, root, f"by{n}", y, 4)
        pairs.append([concat(tools, root, f"common{n}_x", [head, bx, tail]),
                      concat(tools, root, f"common{n}_y", [head, by, tail])])
    groups["common"] = pairs
    # 一部の場面だけ違う（同じ長さ 24 秒のうち 8〜16 秒を差し替え）
    base = clip(tools, root, "pbase", f"testsrc2={S}", 24)
    mid = clip(tools, root, "pmid", f"mandelbrot={S}", 8)
    p1 = root / "_p1.mp4"
    p3 = root / "_p3.mp4"
    ff(tools, "-i", str(base), "-t", "8", "-c:v", "libx264", str(p1))
    ff(tools, "-ss", "16", "-i", str(base), "-c:v", "libx264", str(p3))
    shutil.copy(base, root / "partial_a.mp4")
    groups["partial"] = [["partial_a", concat(tools, root, "partial_b", [p1, mid, p3])]]
    # 音声だけ違う（映像は同じ）。同じ音声の再圧縮も用意する（音声の比較で「一致」と言えるか）
    for name, freq in (("audio_440", 440), ("audio_880", 880)):
        ff(tools, "-f", "lavfi", "-i", f"testsrc2={S}", "-f", "lavfi", "-i", f"sine=frequency={freq}:beep_factor=4",
           "-t", "12", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", str(root / f"{name}.mp4"))
    ff(tools, "-i", str(root / "audio_440.mp4"), "-c:v", "libx264", "-crf", "35", "-c:a", "aac", "-b:a", "48k",
       str(root / "audio_440_re.mp4"))
    ff(tools, "-f", "lavfi", "-i", f"testsrc2={S}", "-t", "12", "-pix_fmt", "yuv420p", "-c:v", "libx264",
       str(root / "audio_none.mp4"))
    groups["audio"] = [["audio_440", "audio_880"], ["audio_440", "audio_440_re"], ["audio_440", "audio_none"]]
    return groups


@contextmanager
def rules(name: str):
    """"before": 変更前の基準（低コントラストは使わない・一致しない場面の数に上限なし）。"after": 今の基準。"""
    saved = {k: getattr(similar, k) for k in ("LOW_STD", "MAX_UNMATCHED")}
    if name == "before":
        similar.LOW_STD = similar.FLAT_STD
        similar.MAX_UNMATCHED = similar.N_POINTS
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(similar, k, v)


def evaluate(groups: dict, sigs: dict) -> dict:
    out = {}

    def same(pair_lists):
        hits, miss = 0, []
        for names in pair_lists:
            for a, b in combinations(names, 2):
                r = similar.compare(sigs[a], sigs[b])
                if r["verdict"] == "similar":
                    hits += 1
                else:
                    miss.append([a, b, r["verdict"], r["matched"], r["informative"]])
        return hits, miss

    def wrongly(pair_lists):
        bad = []
        for names in pair_lists:
            for a, b in combinations(names, 2):
                r = similar.compare(sigs[a], sigs[b])
                if r["verdict"] == "similar":
                    bad.append([a, b, r["matched"], r["informative"]])
        return bad

    h, m = same(groups["reencode"])
    out["再圧縮・解像度違い"] = {"見つけた": h, "組": 27, "見逃し": m}
    h, m = same(groups["low"])
    out["低コントラスト"] = {"見つけた": h, "組": 3 * len(groups["low"]), "見逃し": m}
    out["共通画面が長い別動画（16/20 秒共通）"] = {"類似にした": len(wrongly(groups["common"])), "組": len(groups["common"]),
                                         "詳細": wrongly(groups["common"])}
    out["一部の場面だけ違う"] = {"類似にした": len(wrongly(groups["partial"])), "組": len(groups["partial"])}
    r = similar.compare(sigs["audio_440"], sigs["audio_880"])
    out["音声だけ違う（映像の判定）"] = r["verdict"]
    found = total_pairs = 0
    for low, src in SAME_FOOTAGE.items():
        for a in (f"{low}_orig", f"{low}_lowres", f"{low}_recompress"):
            for b in (f"{src}_orig", f"{src}_lowres", f"{src}_recompress"):
                total_pairs += 1
                found += similar.compare(sigs[a], sigs[b])["verdict"] == "similar"
    out["同じ映像で明るさ・コントラストだけ違う"] = {"見つけた": found, "組": total_pairs}
    # 別の動画どうし: 全素材の代表（orig）と低コントラスト・ノイズの別動画の全組
    names = [g[0] for g in groups["reencode"] + groups["low"]] + groups["negative"] \
        + [p for pair in groups["common"] for p in pair]
    fps, total = [], 0
    for a, b in combinations(names, 2):
        ka, kb = a.split("_")[0], b.split("_")[0]
        if ka == kb or SAME_FOOTAGE.get(ka) == kb or SAME_FOOTAGE.get(kb) == ka:
            continue
        if a.startswith("common") and b.startswith("common") and a.split("_")[0] == b.split("_")[0]:
            continue  # 共通画面の組は上で数える
        total += 1
        if similar.compare(sigs[a], sigs[b])["verdict"] == "similar":
            fps.append([a, b])
    out["別の動画どうし"] = {"誤ってまとめた": len(fps), "組": total, "詳細": fps}
    t0 = time.perf_counter()
    for _ in range(3):
        groups_found, _ = similar.group({i: sigs[n] for i, n in enumerate(sigs)})
    out["グループ化の時間（秒・3回平均）"] = round((time.perf_counter() - t0) / 3, 4)
    out["グループ数"] = len(groups_found)
    return out


def main() -> int:
    tools = media.find_ffmpeg()
    if not tools.available:
        print("FFmpeg が必要です")
        return 1
    argv = sys.argv[1:]
    if "--json" in argv:
        i = argv.index("--json")
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    root = Path(args[0]) if args else Path(tempfile.mkdtemp(prefix="recsort_eval_cases_"))
    root.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    plan_file = root / "_groups.json"
    if plan_file.exists():  # 2回目以降は作った素材を使い直す（判定基準だけを比べ直すとき）
        groups = json.loads(plan_file.read_text(encoding="utf-8"))
    else:
        groups = make_samples(tools, root)
        plan_file.write_text(json.dumps(groups), encoding="utf-8")
    made = time.perf_counter() - t0
    sigs, times = {}, []
    for p in sorted(root.glob("*.mp4")):
        if p.name.startswith("_"):
            continue
        dur = media.probe(tools, str(p))["duration"]
        t1 = time.perf_counter()
        sigs[p.stem] = similar.signature(tools, str(p), dur)
        times.append(time.perf_counter() - t1)
    report = {"動画の本数": len(sigs), "素材の作成（秒）": round(made, 1),
              "代表場面の取り出し（秒/本・平均）": round(sum(times) / len(times), 3)}
    for name in ("before", "after"):
        with rules(name):
            report[name] = evaluate(groups, sigs)
    audio = {}
    for a, b in groups["audio"]:
        t1 = time.perf_counter()
        r = similar.audio_compare(tools, str(root / f"{a}.mp4"), str(root / f"{b}.mp4"),
                                  media.probe(tools, str(root / f"{a}.mp4"))["duration"])
        audio[f"{a} / {b}"] = {"判定": r["verdict"], "一致した区間": r["matched"], "比べた区間": r["compared"],
                               "時間（秒）": round(time.perf_counter() - t1, 2)}
    report["音声の比較（比較画面で使う）"] = audio
    text = json.dumps(report, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(text)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(text, encoding="utf-8")
    if not args:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
