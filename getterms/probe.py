# -*- coding: utf-8 -*-
r"""交付剖面与 term_qc 工作清单（零成本，只读已落盘 dump，不调 API）。

与 `selfcheck` 的分工：
  * `selfcheck` —— **闸门**用的高精确告警（结构性问题、惯用复数被压单数）。
    它的告警数要求为 0，所以只能放进「几乎必定是错」的判据。
  * `probe`（本模块）—— **给人看的剖面**与**交给下游 `term_qc` 的工作清单**。
    这里的每一项都是「值得人看一眼」，**不是错误判定**，所以不做断言、不当闸门。

为什么需要它：西语/俄语不再出「请老师过目」的 xlsx（两位老师已验收，
用户口径是「老师通过有个屁用，跑砸了责任在我们」），但我们自己仍然需要
能看清每一批的样态。这一份就是那个「样态」—— md + 控制台，不做表格。

⚠ 本模块所有判据都是**粗筛**。2026-09-19 的教训：俄语那批看着像「模型编的
音译」（`Dindajeni`、`China Rately Raveus`、`Кен`、`Лу Сини`）—— 逐条回查原句，
**12/14 都逐字出现在俄语原句里**，是上游 ASR / 现场口译的产物，而我们的提示词
本来就要求「上游拼错照抄」。差点据此去改提示词，那会让模型必须写出原句里不存在
的拼写、直接破掉逐字锚定这道反幻觉闸门。**所以这里只报给人看，绝不自动改。**
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from .config import FINAL_DUMPS, ROOT, utf8_stdout
from .corpus import cjk_ratio
from .extract import normalize_delivery_text

LANGS = ("es", "fr", "ru")
LAYER_ORDER = ("conf/poli", "conf/econ", "conf/tech",
               "tour/attr", "tour/muse", "tour/scen", "tour/serv")

_QUOTES_OPEN = "“«‘„‹「"
_QUOTES_CLOSE = "”»’›」"
_QUOTE_ANY = _QUOTES_OPEN + _QUOTES_CLOSE + '"'
_LATIN = re.compile(r"[A-Za-z]")
_CYR = re.compile(r"[Ѐ-ӿ]")
# 罗马数字与常见真品牌：俄语术语里出现拉丁字母不一定是问题
_LATIN_OK = re.compile(r"^(?:[IVXLCDM]+|Grab|COVID-19|[A-Z]{2,6})$")


def _dump_path(lang: str, source: str | None) -> Path:
    if source:
        p = Path(source)
        return p if p.exists() else ROOT / source
    rel = FINAL_DUMPS[lang]
    p = ROOT / "bakeoff" / rel
    return p if p.exists() else ROOT / rel


def load(lang: str, source: str | None = None) -> tuple[list[dict], Path]:
    """读 dump，按**汉字占比**判外语侧（不依赖 `term_*_base` 非空这个脆弱假设）。"""
    path = _dump_path(lang, source)
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("rows", [])
    out: list[dict] = []
    for r in items:
        s_text = r.get("src") or r.get("src_text") or ""
        t_text = r.get("tgt") or r.get("tgt_text") or ""
        fo_src = cjk_ratio(s_text) < cjk_ratio(t_text)
        side = "src" if fo_src else "tgt"
        other = "tgt" if fo_src else "src"
        span = r.get(f"term_{side}_span") or r.get(f"term_{side}") or ""
        base = r.get(f"term_{side}_base") or ""
        out.append({
            "file": r.get("file", ""), "sent_id": r.get("sent_id"),
            "layer": r.get("layer", "?"),
            "zh": r.get(f"term_{other}") or "",
            # ⚠ `deliv` 必须是**归一化之后**的值 —— 那才是王敬真正拿到的东西。
            #   bake-off 的 dump 故意不过 `unify_dict_forms` / `normalize_delivery`
            #   （否则 A/B 不可比），所以这里在内存里补上。
            #   第一版没做，于是「引号不配对」把 `Yan’an` 的**撇号**当成收引号，
            #   一口气报了 5 条假阳性 —— 而交付时它早就被归一化成 `Yan'an` 了。
            "span": span, "base": base,
            "deliv": normalize_delivery_text(base or span),
            "fo_text": s_text if fo_src else t_text,
            "zh_text": t_text if fo_src else s_text,
            "types": str(r.get("types") or ""),
        })
    return out, path


def _loc(r: dict) -> str:
    return f"{r['file'].replace('_align.qc.json', '')}#{r['sent_id']}"


# ---------------------------------------------------------------- 各项粗筛

def unbalanced_quotes(rows: list[dict]) -> list[str]:
    """交付值里的引号不配对。上游原句就这样时**不该自动修**，交人判。"""
    out = []
    for r in rows:
        d = r["deliv"]
        q = [c for i, c in enumerate(d) if c in _QUOTE_ANY
             # 夹在字母之间的 ’ 是撇号不是引号（Yan’an、d’entreprise）
             and not (c == "’" and 0 < i < len(d) - 1
                      and d[i - 1].isalpha() and d[i + 1].isalpha())]
        if not q:
            continue
        straight = sum(1 for c in q if c == '"')
        curly_o = sum(1 for c in q if c in _QUOTES_OPEN)
        curly_c = sum(1 for c in q if c in _QUOTES_CLOSE)
        bad = (straight % 2) or (curly_o != curly_c)
        if bad:
            in_sent = d in r["fo_text"]
            out.append(f"{_loc(r)} {r['zh']} = {d!r}"
                       + ("（原句就是这样 -> 上游问题）" if in_sent else "（原句里没有）"))
    return out


def latin_in_cyrillic(rows: list[dict]) -> list[str]:
    """俄语交付值里残留拉丁字母。多为上游音译，交 term_qc 与上游。"""
    out = []
    for r in rows:
        d = r["deliv"]
        if not _CYR.search(d) and not any(c.isalpha() for c in d):
            continue
        lat = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]*", d)
               if not _LATIN_OK.match(w)]
        if lat and _LATIN.search(d):
            in_sent = all(w in r["fo_text"] for w in lat)
            out.append(f"{_loc(r)} {r['zh']} = {d!r}  拉丁串 {lat}"
                       + ("（原句就有 -> 上游 ASR）" if in_sent else "（**原句里没有**）"))
    return out


def zh_to_many(rows: list[dict], min_n: int = 2, max_zh_len: int = 6) -> list[str]:
    """同一个中文术语给出多个外语交付值。短术语（多为专名）最值得看。"""
    g: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    where: dict[str, set] = collections.defaultdict(set)
    for r in rows:
        if r["zh"] and r["deliv"]:
            g[r["zh"]][r["deliv"]] += 1
            where[r["zh"]].add(r["file"])
    out = []
    for zh, c in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(c) < min_n or len(zh) > max_zh_len:
            continue
        vals = " ｜ ".join(f"{v}×{n}" for v, n in c.most_common())
        cross = "跨文件" if len(where[zh]) > 1 else "同文件"
        out.append(f"{zh}（{len(c)} 个值，{cross}）: {vals}")
    return out


def many_to_zh(rows: list[dict], min_n: int = 3) -> list[str]:
    """同一个外语交付值对应多个中文术语。这个方向以前**完全没有检查**。"""
    g: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["zh"] and r["deliv"]:
            g[r["deliv"]][r["zh"]] += 1
    out = []
    for fo, c in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(c) < min_n:
            continue
        out.append(f"{fo}（{len(c)} 个中文）: "
                   + " ｜ ".join(f"{v}×{n}" for v, n in c.most_common()))
    return out


def span_dominates_sentence(rows: list[dict], ratio: float = 0.6) -> list[str]:
    """切片占了原句 60% 以上 —— 过抽信号。与 `looks_like_clause` 不同量。"""
    out = []
    for r in rows:
        for txt, sp, tag in ((r["fo_text"], r["span"], "外"),
                             (r["zh_text"], r["zh"], "中")):
            if not txt or not sp:
                continue
            k = len(sp) / len(txt)
            if k >= ratio:
                out.append(f"{_loc(r)} [{tag} {k:.0%}] {sp!r}  原句 {txt[:60]!r}")
    return sorted(out, key=lambda s: -float(re.search(r"(\d+)%", s).group(1)))


def length_ratio_extreme(rows: list[dict], lo: float = 1.6, hi: float = 11.0) -> list[str]:
    """中外长度比极端 -> 疑似错配或粒度不符。纯粗筛，缩略语会假阳性。"""
    out = []
    for r in rows:
        if not r["zh"] or not r["deliv"]:
            continue
        k = len(r["deliv"]) / len(r["zh"])
        if k > hi or k < lo:
            out.append(f"{_loc(r)} 比值 {k:.1f}  {r['zh']} = {r['deliv']!r}")
    return sorted(out, key=lambda s: -float(re.search(r"比值 ([\d.]+)", s).group(1)))


def types_cross_language(by_lang: dict[str, list[dict]]) -> list[str]:
    """同一中文术语在一个语种标 other、在另一个语种标了具体类。"""
    concrete: dict[str, set] = collections.defaultdict(set)
    onlyother: dict[str, set] = collections.defaultdict(set)
    for lg, rows in by_lang.items():
        for r in rows:
            ts = {x for x in r["types"].split(",") if x}
            if not ts or not r["zh"]:
                continue
            if ts == {"other"}:
                onlyother[r["zh"]].add(lg)
            else:
                concrete[r["zh"]] |= {f"{lg}:{'/'.join(sorted(ts - {'other'}))}"}
    out = []
    for zh, lgs in sorted(onlyother.items()):
        if zh in concrete:
            out.append(f"{zh}  在 {'/'.join(sorted(lgs))} 标纯 other，"
                       f"但在 {' '.join(sorted(concrete[zh]))}")
    return out


def other_mixed(rows: list[dict]) -> list[str]:
    """`other` 与其他类混用 —— 违反提示词自己定的互斥规则。"""
    return [f"{_loc(r)} {r['zh']} = {r['deliv']!r}  types={r['types']}"
            for r in rows
            if "other" in r["types"].split(",") and len(
                [x for x in r["types"].split(",") if x]) > 1]


def dead_both_sides(rows: list[dict]) -> list[str]:
    """两侧都像废值：中文侧无常用字且外语侧无词典词（上游 ASR 塌了）。"""
    out = []
    for r in rows:
        zh, d = r["zh"], r["deliv"]
        if len(zh) < 4 or len(d) < 10:
            continue
        # 极粗的判据：外语侧单词数 <= 4 且总长 > 30（长复合怪词），或中文侧含明显乱码搭配
        long_odd = len(d) > 40 and len(d.split()) <= 3
        if long_odd:
            out.append(f"{_loc(r)} {zh} = {d!r}（外语侧是超长复合词，疑似 ASR 塌缩）")
    return out


# ---------------------------------------------------------------- 剖面

def layer_table(rows: list[dict]) -> list[tuple]:
    g: dict[str, dict] = collections.defaultdict(
        lambda: {"terms": 0, "sent": set(), "files": set()})
    for r in rows:
        d = g[r["layer"]]
        d["terms"] += 1
        d["sent"].add((r["file"], r["sent_id"]))
        d["files"].add(r["file"])
    out = []
    for lay in sorted(g, key=lambda x: (LAYER_ORDER.index(x)
                                        if x in LAYER_ORDER else 99, x)):
        d = g[lay]
        out.append((lay, len(d["files"]), len(d["sent"]), d["terms"],
                    d["terms"] / max(1, len(d["sent"]))))
    return out


def types_dist(rows: list[dict]) -> list[tuple]:
    c = collections.Counter()
    for r in rows:
        for x in r["types"].split(","):
            if x:
                c[x] += 1
    n = len(rows) or 1
    return [(k, v, v / n) for k, v in c.most_common()]


CHECKS = (
    ("other 与其他类混用（违反互斥规则）", other_mixed,
     "全量跑里 `norm_types` 已在交付时自动丢掉 other；这里报的是 bake-off dump 里"
     "**模型的原始输出**，留着是为了看模型行为有没有变差"),
    ("引号不配对", unbalanced_quotes, "上游原句就这样时不自动修，交人判"),
    ("俄语交付值残留拉丁字母", latin_in_cyrillic, "多为上游 ASR 音译"),
    ("同一中文 -> 多个外语值", zh_to_many, "专名音译不一致最值得看"),
    ("同一外语 -> 多个中文", many_to_zh, "以前完全没有这个方向的检查"),
    ("切片占原句 ≥60%", span_dominates_sentence, "过抽信号"),
    ("中外长度比极端", length_ratio_extreme, "疑似错配或粒度不符；缩略语会假阳性"),
    ("两侧疑似废值", dead_both_sides, "上游 ASR 塌缩"),
)


def render(lang: str, rows: list[dict], path: Path, limit: int,
           cross: list[str] | None = None) -> str:
    L: list[str] = []
    a = L.append
    a(f"# 交付剖面 · {lang}")
    a("")
    a(f"dump `{path.name}`　术语 **{len(rows)}** 条　"
      f"文件 {len({r['file'] for r in rows})}　"
      f"有术语句 {len({(r['file'], r['sent_id']) for r in rows})}")
    a("")
    a("⚠ 下面每一项都是**粗筛、供人看**，不是错误判定。闸门用的是 `selfcheck`。")
    a("")
    a("## 分层密度")
    a("")
    a("| 层 | 文件 | 有术语句 | 术语 | 密度 |")
    a("|---|---|---|---|---|")
    for lay, nf, ns, nt, dens in layer_table(rows):
        a(f"| {lay} | {nf} | {ns} | {nt} | {dens:.2f} |")
    a("")
    a("## 类型分布")
    a("")
    a("| 类型 | 条数 | 占比 |")
    a("|---|---|---|")
    for k, v, p in types_dist(rows):
        a(f"| {k} | {v} | {p:.1%} |")
    a("")
    for title, fn, note in CHECKS:
        if fn is latin_in_cyrillic and lang != "ru":
            continue
        items = fn(rows)
        a(f"## {title} — {len(items)} 条")
        a("")
        a(f"*{note}*")
        a("")
        if not items:
            a("（无）")
        else:
            for x in items[:limit]:
                a(f"- {x}")
            if len(items) > limit:
                a(f"- …另有 {len(items) - limit} 条（`--limit` 调大可看全）")
        a("")
    if cross:
        a(f"## 跨语种 types 不一致 — {len(cross)} 条")
        a("")
        a("*同一中文术语在一个语种标纯 `other`、在另一个语种标了具体类*")
        a("")
        for x in cross[:limit]:
            a(f"- {x}")
        if len(cross) > limit:
            a(f"- …另有 {len(cross) - limit} 条")
        a("")
    return "\n".join(L)


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(
        prog="getterms.probe",
        description="交付剖面与 term_qc 工作清单（零成本，只读 dump）")
    ap.add_argument("--lang", choices=list(LANGS), default=None)
    ap.add_argument("--all", action="store_true", help="三语都跑，并做跨语种 types 比对")
    ap.add_argument("--source", default=None, help="dump 路径；不给则用该语种定稿 dump")
    ap.add_argument("--limit", type=int, default=15, help="每类最多列几条")
    ap.add_argument("--md", default=None,
                    help="把剖面写到这个 md（不给则只打印到控制台）")
    a = ap.parse_args(argv)

    langs = list(LANGS) if a.all else ([a.lang] if a.lang else ["es"])
    by_lang: dict[str, list[dict]] = {}
    paths: dict[str, Path] = {}
    for lg in langs:
        by_lang[lg], paths[lg] = load(lg, a.source if len(langs) == 1 else None)
    cross = types_cross_language(by_lang) if len(by_lang) > 1 else None

    for lg in langs:
        md = render(lg, by_lang[lg], paths[lg], a.limit,
                    cross if lg == langs[-1] else None)
        print(md)
        print()
        if a.md:
            p = Path(a.md) if len(langs) == 1 else Path(a.md).with_name(
                Path(a.md).stem + f"_{lg}" + Path(a.md).suffix)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(md, encoding="utf-8", newline="\n")
            print(f"[写出] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
