"""术语质量信号：从 bake-off 的 terms_*.json 里算出「过抽/漏抽」的可量化指标。

这些指标来自 2026-09-16 的一次人工对比发现：DeepSeek 比 Gemini 多抽 143 条，
但多出来的基本是普通词（dignidad/尊严、Estado/国家、elecciones/选举）和整个从句
（`el derecho de cada persona a expresar libremente su opinión`/「每个人自由表达
意见的权利」这类把整句当术语的情况），同时漏掉了高价值术语（人名、政体名、
疫情名这类专有说法）。
把这个洞察做成自动指标，法语/俄语轮次就不用再手工翻一遍。

用法：
  python -m getterms.quality --final                  # 只看三语**定稿 dump**
  python -m getterms.quality --files a.json b.json    # 指定两份做 A/B
  python -m getterms.quality --field span             # 量原句切片而不是交付值
  python -m getterms.quality                          # 无参数 = bakeoff/ 下**全部**

⚠ 无参数时它 glob `bakeoff/terms_*.json`，会把**所有版本、所有语种**混在一张表里
  按 other 占比排序 —— 拿它比「改提示词前后」很容易读错行。要做 A/B 就用 `--files`。

⚠ 量的是哪一列：`bakeoff.py` 写 dump 时 `term_src`/`term_tgt` 取 `out_src`/`out_tgt`
  = 有词典形就用词典形，所以 **v5 及更早它是原句切片、v6 起是交付值**。
  本模块默认 `--field delivered`（我们真正交出去的东西）。`dup_ratio` 对这个选择
  **敏感**：`brotes de bambú` 与 `brote de bambú` 两条切片还原后会并成一条。
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
from pathlib import Path

from .config import BAKEOFF_DIR, FINAL_DUMPS, utf8_stdout

# 「像从句而不像术语」的长度阈值：中文按字符、西语按词数
ZH_LONG_CHARS = 12
LATIN_LONG_WORDS = 5


def _is_cjk(s: str) -> bool:
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return False
    return sum(1 for c in chars if "一" <= c <= "鿿") / len(chars) > 0.3


def looks_like_clause(term: str) -> bool:
    """长得像从句/短语而不像术语。"""
    t = term.strip()
    if not t:
        return False
    if _is_cjk(t):
        return len(t) > ZH_LONG_CHARS
    return len(t.split()) > LATIN_LONG_WORDS


def has_clause_punct(term: str) -> bool:
    """含逗号/顿号 —— 术语里几乎不该出现。"""
    return any(ch in term for ch in ",，、;；")


def pick(r: dict, field: str) -> tuple[str, str]:
    """取 (中文侧原样的 term_src, term_tgt)，按 field 决定用切片还是交付值。

    注意这里不判方向，只是把两列取出来 —— 下游指标（长度/标点/重复）两侧都算。
    """
    if field == "span":
        return (r.get("term_src_span") or r["term_src"],
                r.get("term_tgt_span") or r["term_tgt"])
    return r["term_src"], r["term_tgt"]


def analyse(rows: list[dict], field: str = "delivered") -> dict:
    n = len(rows)
    if n == 0:
        return {}
    vals = [pick(r, field) for r in rows]
    types = collections.Counter(
        t.strip() for r in rows for t in str(r["types"]).split(",") if t.strip())
    clause_src = sum(1 for s, _t in vals if looks_like_clause(s))
    clause_tgt = sum(1 for _s, t in vals if looks_like_clause(t))
    punct = sum(1 for s, t in vals if has_clause_punct(s) or has_clause_punct(t))
    other_mixed = sum(1 for r in rows
                      if "other" in str(r["types"]) and "," in str(r["types"]))
    poli_rows = [r for r in rows if r.get("layer") == "conf/poli"]
    poli_tab = sum(1 for r in poli_rows if "tab" in str(r["types"]))
    uniq = len(set(vals))
    return {
        "terms": n,
        "field": field,
        "has_span_cols": bool(rows) and "term_src_span" in rows[0],
        "unique_pairs": uniq,
        "dup_ratio": 1 - uniq / n,
        "other_share": types["other"] / n,
        "tab_count": types["tab"],
        "hot_count": types["hot"],
        "clause_like": max(clause_src, clause_tgt),
        "clause_share": max(clause_src, clause_tgt) / n,
        "punct_in_term": punct,
        "other_mixed": other_mixed,
        "conf_poli_terms": len(poli_rows),
        "conf_poli_tab": poli_tab,
        "conf_poli_tab_share": poli_tab / len(poli_rows) if poli_rows else 0.0,
        "types": dict(types.most_common()),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.quality")
    ap.add_argument("--files", nargs="*", default=None,
                    help="要比的 terms_*.json；不给则 glob bakeoff/ 下全部")
    ap.add_argument("--final", action="store_true",
                    help="只看 config.FINAL_DUMPS 三语定稿 dump")
    ap.add_argument("--field", choices=["span", "delivered"], default="delivered",
                    help="量原句切片还是交付值（默认交付值）；dup_ratio 对此敏感")
    ap.add_argument("--pairwise", action="store_true",
                    help="打两两重合率表（label 多时很宽，默认只在 ≤6 份时自动打）")
    a = ap.parse_args(argv)
    utf8_stdout()

    if a.final:
        files = [pathlib.Path(v) for v in FINAL_DUMPS.values()]
    elif a.files:
        files = [pathlib.Path(f) if pathlib.Path(f).exists() else BAKEOFF_DIR / f
                 for f in a.files]
    else:
        files = sorted(BAKEOFF_DIR.glob("terms_*.json"))
        print("⚠ 无参数：下表混了 bakeoff/ 下**全部版本与全部语种**，"
              "按 other 占比排序，别拿它读 A/B。要 A/B 用 --files，要定稿用 --final。")
    files = [p for p in files if p.exists()] or []
    if not files:
        print(f"没有可读的 terms_*.json（{BAKEOFF_DIR}）")
        return 1
    stats: dict[str, dict] = {}
    for p in files:
        label = p.stem[len("terms_"):] if p.stem.startswith("terms_") else p.stem
        stats[label] = analyse(json.loads(p.read_text(encoding="utf-8")), a.field)

    fld = {"span": "原句切片 term_*_span", "delivered": "交付值 term_src/term_tgt"}
    print(f"术语质量信号（越低越好的用 ↓ 标出）｜量的是：{fld[a.field]}")
    for lb, s in stats.items():
        if s and a.field == "span" and not s["has_span_cols"]:
            print(f"  ⚠ {lb}: 这份 dump 没有 term_*_span 列（v5 及更早），已回落到 term_src/term_tgt")
    print("=" * 128)
    print(f"{'参赛':40s} {'术语':>5s} {'other占比↓':>10s} {'像从句↓':>8s} "
          f"{'含逗号↓':>8s} {'other混用↓':>10s} {'重复↓':>7s} "
          f"{'tab':>5s} {'hot':>5s} {'poli层tab占比':>13s}")
    print("-" * 128)
    for lb, s in sorted(stats.items(), key=lambda kv: kv[1].get("other_share", 1)):
        if not s:
            continue
        print(f"{lb:40s} {s['terms']:5d} {s['other_share']:9.1%} "
              f"{s['clause_share']:7.1%} {s['punct_in_term']:8d} "
              f"{s['other_mixed']:10d} {s['dup_ratio']:6.1%} "
              f"{s['tab_count']:5d} {s['hot_count']:5d} "
              f"{s['conf_poli_tab_share']:12.1%}")

    print()
    print("怎么读这张表：")
    print("  other 占比高 = 认不出领域，把术语倒进 other；实测 12% 左右属正常，21% 属过抽")
    print("  像从句 / 含逗号 = 抽的不是术语而是短语或整句，下游要人工删")
    print("  other 混用 = 违反 other 互斥规则（other 定义是「归不进其他类」）")
    print("  poli 层 tab 占比 = 时政语料里敏感词标注是否充分；**过低是安全过滤悄悄漏抽的信号**")

    # 两两重合率：定位系统性怪癖（计划 §4.4 的判据之一）
    keyed = {}
    for p in files:
        lb = p.stem[len("terms_"):] if p.stem.startswith("terms_") else p.stem
        rows = json.loads(p.read_text(encoding="utf-8"))
        keyed[lb] = {(r["file"], str(r["sent_id"])) + pick(r, a.field) for r in rows}
    labels = sorted(keyed)
    if len(labels) > 1 and (a.pairwise or len(labels) <= 6):
        print()
        print("两两重合率（行 ∩ 列 / 行）—— 低重合说明两者取向不同，值得人工看分歧")
        w = max(len(l) for l in labels) + 1
        print(" " * w + "".join(f"{l[:14]:>15s}" for l in labels))
        for a in labels:
            cells = []
            for b in labels:
                inter = len(keyed[a] & keyed[b])
                cells.append(f"{inter / len(keyed[a]):14.0%} " if keyed[a] else " " * 15)
            print(f"{a:{w}s}" + "".join(cells))

    out = BAKEOFF_DIR / "quality_signals.json"
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n机读结果: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
