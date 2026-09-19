"""术语 span 缺陷剖面：同一套判据横向比三语，改提示词后拿它做回归。

只看「不依赖领域知识就能判定为错」的类别，不做质量评分：
  de        中文侧把「的」修饰语裹进 span（词内「目的」「的确」不算）
  lead      外语侧以冠词/介词开头（西/法：冠词；俄：前置词）
  trail     外语侧以功能词结尾（span 被切断的迹象）
  punct     span 内含逗号/顿号（跨并列项或跨同位语）
  mutil     畸形并列：首冠词被砍、内部冠词还在（paix et le développement）。
            首词指标看不见这一类；两侧都带冠词（la paz y el desarrollo）不算
  frag      只作为嵌套子串出现、从不独立成词的碎片（趵突泉 -> 趵突）
  1char     单字中文术语
  long      过长 span（中文 >=15 字 / 外语 >=8 词），像从句
  dupsent   同一句内完全重复的条目

用法:
  python -m getterms.span_defects                     # 默认跑三语**定稿 dump**
  python -m getterms.span_defects --field delivered   # 量交付值而不是原句切片
  python -m getterms.span_defects bakeoff/terms_a.json bakeoff/terms_b.json

⚠ 两条必须显式维护的事实（2026-09-18 各栽一次）：

1. **默认目标指向哪个版本。** 原来默认写的是**无版本后缀的 v0 基线**，
   不带参数跑会「成功」并打出一张关于旧版本的表，不报任何警。现已改为读
   `config.FINAL_DUMPS`（与 `selfcheck` 共用），并在表头打印文件名。

2. **量的是哪一列。** `bakeoff.py` 写 dump 时 `term_src`/`term_tgt` 取
   `out_src`/`out_tgt` = 有词典形就用词典形，所以 **v5 及更早它是原句切片，
   v6 起它是交付值**，切片另存在 `term_*_span`。本模块的判据（`lead`/`trail`/
   `punct`/`long`…）量的是**抽取跨度的形状**，所以默认 `--field span`：
   有 `term_*_span` 就用它，没有才回落到 `term_src`/`term_tgt` 并在表头写明。
   `--field delivered` 量交付值，用来看「还原之后交出去的东西长什么样」。
   同一份 v6 dump 用两种 `--field` 跑出的数**本就应该不同**；如果相同，
   说明回落了或者这份 dump 没有词典形。
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

from .config import BAKEOFF_DIR, FINAL_DUMPS, utf8_stdout
from .corpus import cjk_ratio

LEAD = {
    "es": r"^(el|la|los|las|un|una|unos|unas|del|de|al|a|en|por|para|con|su|sus)\b",
    "fr": r"^(le|la|les|l'|un|une|des|du|de|au|aux|à|en|son|sa|ses|ce|cette|cet)\b",
    "ru": r"^(в|во|на|с|со|для|от|из|по|к|ко|о|об|при|за|до|у|над|под|через|между)\b",
}
TRAIL = {
    "es": r"\b(de|del|la|el|y|en|a|con|por)$",
    "fr": r"\b(de|du|des|la|le|les|et|en|à|d')$",
    "ru": r"\b(и|в|на|с|для|от|по)$",
}
DE = re.compile(r"(?<!目)的(?![确话])")
# 并列连词 / 逗号之后紧跟冠词或缩合形式 —— 前置冠词被砍掉后留下的畸形并列
INNER = re.compile(
    r"(?:\b(?:et|y|e|o|ou|и)\s+|,\s*)"
    r"(?:le|la|les|l'|du|de\s+la|de\s+l'|des|au|aux|el|los|las|del|un|una)\b",
    re.I)
PUNCT = re.compile(r"[，,；;、]")


def sides(r: dict, field: str = "delivered") -> tuple[str, str]:
    """返回 (中文侧, 外语侧)。

    field="span"      -> 优先取 `term_*_span`（原句切片），缺列时回落
    field="delivered" -> 取 `term_src`/`term_tgt`（v6 起 = 词典形，v5 及更早 = 切片）
    """
    src, tgt = r["term_src"], r["term_tgt"]
    if field == "span":
        src = r.get("term_src_span") or src
        tgt = r.get("term_tgt_span") or tgt
    if cjk_ratio(src) > cjk_ratio(tgt):
        return src, tgt
    return tgt, src


def has_span_cols(rows: list[dict]) -> bool:
    """这份 dump 带不带显式的切片列（v6 起才有）。"""
    return bool(rows) and "term_src_span" in rows[0]


def profile(rows: list[dict], lang: str, field: str = "delivered") -> dict:
    n = len(rows)
    zh = [sides(r, field)[0] for r in rows]
    fo = [sides(r, field)[1] for r in rows]
    lead = re.compile(LEAD[lang], re.I)
    trail = re.compile(TRAIL[lang], re.I)

    hits: dict[str, list[str]] = collections.defaultdict(list)
    for z, f in zip(zh, fo):
        if DE.search(z):
            hits["de"].append(z)
        if lead.match(f.strip()):
            hits["lead"].append(f)
        if trail.search(f.strip()):
            hits["trail"].append(f)
        if INNER.search(f) and not lead.match(f.strip()):
            # 首冠词被砍掉、内部冠词还留着 -> 畸形并列，术语库不能直接用
            hits["mutil"].append(f)
        if PUNCT.search(z) or PUNCT.search(f):
            hits["punct"].append(f"{z} | {f}")
        if len(z) == 1:
            hits["1char"].append(z)
        if len(z) >= 15 or len(f.split()) >= 8:
            hits["long"].append(f"{z} | {f}")

    # 句内嵌套与碎片：child 只作为别人的子串出现，从未独立成条
    by_sent: dict[tuple, list[str]] = collections.defaultdict(list)
    for r, z in zip(rows, zh):  # noqa: B905 —— zh 与 rows 同长
        by_sent[(r["file"], str(r["sent_id"]))].append(z)
    nested_child: collections.Counter = collections.Counter()
    standalone: collections.Counter = collections.Counter()
    for v in by_sent.values():
        for a in v:
            covered = any(a != b and a in b for b in v)
            (nested_child if covered else standalone)[a] += 1
    hits["frag"] = sorted(t for t in nested_child if t not in standalone and len(t) <= 4)
    hits["nested"] = sorted(nested_child)

    for v in by_sent.values():
        for t, c in collections.Counter(v).items():
            if c > 1:
                hits["dupsent"].extend([t] * (c - 1))

    other = [f"{z}|{f}" for r, z, f in zip(rows, zh, fo) if "other" in r["types"]]
    return {"n": n, "hits": hits, "other": other, "field": field,
            "has_span": has_span_cols(rows),
            "types": collections.Counter(
                t for r in rows for t in re.split(r"[,\s]+", r["types"]) if t)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.span_defects")
    ap.add_argument("--show", default="de,lead,punct,frag,dupsent,long",
                    help="要列出实例的类别，逗号分隔；all 表示全列")
    ap.add_argument("--limit", type=int, default=12, help="每类最多列几条")
    ap.add_argument("--field", choices=["span", "delivered"], default="span",
                    help="量原句切片（span，默认）还是交付值（delivered）。"
                         "见模块 docstring 第 2 条 —— 这两个是不同的量。")
    ap.add_argument("files", nargs="*",
                    help="terms_*.json 路径；默认 config.FINAL_DUMPS 三语定稿 dump")
    a = ap.parse_args(argv)
    utf8_stdout()

    if a.files:
        targets = [(("ru" if "__ru" in f else "fr" if "__fr" in f else "es"), f)
                   for f in a.files]
    else:
        # 默认目标集中登记在 config.FINAL_DUMPS，别再在这里硬编码版本
        targets = [(lg, str(p)) for lg, p in FINAL_DUMPS.items()]

    profs = {}
    for lang, fn in targets:
        p = pathlib.Path(fn)
        if not p.is_absolute() and not p.exists():
            p = BAKEOFF_DIR / fn
        if not p.exists():
            print(f"跳过（不存在）: {p}")
            continue
        # ⚠ 键必须含文件名：先前用 lang 做键，同一语种传两个文件（做 A/B 对照时
        #   恰恰就是这种用法）会**互相覆盖，只显示后一个**。2026-09-18 实测到。
        # ⚠ 2026-09-18 二次修：前一版用 fn[:28]，而 A/B 对照的两个文件名
        #   前 28 字符恰好都是 "terms_gemini-3_7-flash__high"，**仍然互相覆盖**。
        #   改用完整 stem。
        label = (lang if len(targets) == len(set(l for l, _ in targets))
                 else f"{lang}:{pathlib.Path(fn).stem}")
        rows = json.loads(p.read_text(encoding="utf-8"))
        profs[label] = profile(rows, lang, a.field)
        profs[label]["src"] = p.name

    cats = ["de", "lead", "mutil", "trail", "punct", "frag", "1char", "long",
            "dupsent"]
    print("span 缺陷剖面（占该语种术语总数的比例）")
    fld = {"span": "原句切片 term_*_span", "delivered": "交付值 term_src/term_tgt"}
    print(f"量的是：{fld[a.field]}")
    for lg, pr in profs.items():
        note = "" if pr["has_span"] else "  ⚠ 这份 dump 没有 term_*_span 列，已回落到 term_src/term_tgt"
        print(f"  {lg:<10s} {pr['src']}{note}")
    print("=" * 96)
    # label 可能很长（A/B 对照时带完整 stem），表头要截断，否则列对不上
    print(f"{'类别':10s}" + "".join(f"{lg[-24:]:>26s}" for lg in profs))
    for c in cats:
        row = f"{c:10s}"
        for lg, pr in profs.items():
            k = len(pr["hits"].get(c, []))
            row += f"{k:>14d} ({k / pr['n']:>6.1%})"
        print(row)
    print(f"{'other':10s}" + "".join(
        f"{len(pr['other']):>14d} ({len(pr['other']) / pr['n']:>6.1%})"
        for pr in profs.values()))
    print(f"{'总条数':10s}" + "".join(f"{pr['n']:>23d}" for pr in profs.values()))

    show = cats if a.show == "all" else a.show.split(",")
    for lg, pr in profs.items():
        print()
        print("=" * 96)
        print(f"[{lg}] 实例")
        for c in show:
            v = pr["hits"].get(c, [])
            if v:
                print(f"  {c} ({len(v)}): " + " / ".join(v[:a.limit]))
        print(f"  other ({len(pr['other'])}): " + " / ".join(pr["other"][:30]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
