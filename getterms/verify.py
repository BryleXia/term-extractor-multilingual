"""交付口径校验：把产出的 xlsx 与 HTML 工具的契约逐项机器比对。

用法：python -m getterms.verify out/smoke [--lang es|fr|ru]
检查项（计划 §9）：
  * 列名与列序、sheet 名
  * 文件名规则（replaceOutName 的逐字复刻）
  * 每行的 term_src/term_tgt 都能在同行的 src_text/tgt_text 里定位
  * types 全部属于 10 类白名单，且不含 num
  * note 列恒空

**外语侧是例外（2026-09-18 起三语都是）**：术语列交**词典形**，按定义就不一定是原句
子串（ISO 10241-1 §6.2.2「名词用单数」+ IATE 手册「除非习惯用复数」；俄语再加还原到
主格）。所以外语侧改判「是不是那个切片的合法还原」，并把结果分成
「精确命中 / 合法词典形还原 / 两者都不是」三档 —— 只有第三档才是真问题。
**中文侧仍然要求精确命中，一点都不放宽。**
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from .config import ALLOWED_TYPES
from .corpus import cjk_ratio, out_name_for, replace_out_name
from .extract import (DICT_FORM_LANGS, check_lemma_romance, check_nominative,
                      find_verbatim, load_lemmatizer, load_morph, morph_status)
from .writer import DEFAULT_EXPORT

# ⚠ **冻结的字面量，不是从 writer.DEFAULT_EXPORT 派生**。
#   这里代表「规范长什么样」，是判据；派生出来的话，改错了配置它也跟着错、
#   永远绿（`tests.py` 那句自指的列名断言就是这么失效的）。测试里另有一条
#   把 `DEFAULT_EXPORT.columns` 与这份字面量对齐。
#
# 2026-09-19 定稿：7 列 → 9 列。前 7 列的**位置**与飞书 2.8/2.9 样例逐字一致，
# 两个新列追加在末尾（第 4/5 列 = 句中原始切片，第 8/9 列 = 词条形式）。
# ⚠ 第 6 列列名是 `types`（跟 HTML 工具），与 2.8/2.9 样例的 `type` **不同名** ——
#   三份材料三个名字（HTML `types` / 2.8、2.9 `type` / 2.81 `term_type`），
#   2026-09-19 用户拍板跟工具。名字不同不影响「前 7 列位置一致」这条。
EXPECTED_COLUMNS = ["sent_id", "src_text", "tgt_text",
                    "term_src", "term_tgt", "types", "note",
                    "term_src_dict", "term_tgt_dict"]
EXPECTED_SHEET = "terms"


def _dict_form_ok(base: str, span: str, lang: str, analyzer) -> str | None:
    """按语种分派词典形校验。返回错误说明，None = 通过。"""
    if lang == "ru":
        return check_nominative(base, span, morph=analyzer)
    return check_lemma_romance(base, span, lang, lemmatizer=analyzer)


def load_span_index(out_dir: Path) -> tuple[dict, str]:
    """从 terms_raw.json 建「(句中切片) -> (词条形式)」索引。返回 (索引, 说明)。

    ⚠ 键值是按 **xlsx 的第 4/5 列（句中切片）** 组织的 —— 与 raw 里的
    `term_*_span` 对应；值是 raw 里的 `term_src`/`term_tgt`（= 词条形式），
    与 xlsx 的第 8/9 列对应。**两边名字故意不一样**：raw 的字段名是历史包袱
    （`term_src` 一直是「词条形式」），改它会静默打断 `qc_workbook` /
    `selfcheck` / `probe` 三个模块，所以在这里做显式映射而不是改名。

    两个用途：
      1. 核对 xlsx 的第 8/9 列与 raw 是否同源（**xlsx 与 raw 不同源**比
         「还原对不对」更严重，以前完全没有这项）；
      2. `no_raw` 计数 —— xlsx 有这行、raw 没有。

    形态合法性的判定**不再需要它**：句中切片现在就在 xlsx 第 4/5 列里，
    `_dict_form_ok(词条形式, 切片)` 直接可算，不必再滑窗猜切片。
    """
    rawp = out_dir / "terms_raw.json"
    if not rawp.exists():
        return {}, f"没有 {rawp.name}，无法核对交付值与原始行是否同源"
    try:
        d = json.loads(rawp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"{rawp.name} 读不了（{type(e).__name__}），无法核对同源"
    idx = {}
    for r in d.get("rows", []):
        key = (str(r.get("out_name") or ""), str(r.get("sent_id")),
               str(r.get("term_src_span") or ""), str(r.get("term_tgt_span") or ""))
        idx[key] = (str(r.get("term_src") or ""), str(r.get("term_tgt") or ""))
    return idx, f"{rawp.name}：{len(idx)} 行「句中切片 -> 词条形式」已索引"


def check_xlsx(path: Path, lang: str = "es",
               span_idx: dict | None = None) -> tuple[list[str], dict]:
    """返回 (问题列表, 词典形统计)。不收词典形的语种统计为空。"""
    problems: list[str] = []
    stats = {"exact": 0, "morph": 0, "neither": 0, "why": [], "no_raw": 0}
    wb = load_workbook(path, read_only=True)
    if wb.sheetnames != [EXPECTED_SHEET]:
        problems.append(f"sheet 名应为 ['{EXPECTED_SHEET}']，实际 {wb.sheetnames}")
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        problems.append("空表")
        return problems, stats
    header = [c for c in rows[0]]
    if header != EXPECTED_COLUMNS:
        problems.append(f"表头不符\n  期望 {EXPECTED_COLUMNS}\n  实际 {header}")
    col = {name: i for i, name in enumerate(header)}
    dict_form = lang in DICT_FORM_LANGS
    analyzer = (load_morph() if lang == "ru"
                else load_lemmatizer() if dict_form else None)
    # 按名取列（不用硬编码下标 —— `tests.py:191` 那种 [6] 会在加列时静默读错列）
    i_ts, i_tt = col.get("term_src"), col.get("term_tgt")
    i_ds, i_dt = col.get("term_src_dict"), col.get("term_tgt_dict")
    i_ty, i_note, i_sid = col.get("types"), col.get("note"), col.get("sent_id")
    n_bad_src = n_bad_tgt = n_bad_type = n_note = n_empty_dict = 0

    def _at(r, i):
        return str(r[i] or "") if i is not None and i < len(r) else ""

    for r in rows[1:]:
        src, tgt = _at(r, col.get("src_text")), _at(r, col.get("tgt_text"))
        ts, tt = _at(r, i_ts), _at(r, i_tt)          # 第 4/5 列 = 句中原始切片
        ds, dt = _at(r, i_ds), _at(r, i_dt)          # 第 8/9 列 = 词条形式

        # ---- 门禁 A：第 4/5 列必须逐字出现在各自整句里。
        # `final.json` 的内联标注（`当前[世界经济]{term}面临…`）靠这一步在原句里
        # 定位；定位不到就等于标注不出来。**这是 2026-09-19 改列的全部意义**，
        # 所以单独成档、不混进形态判定里。
        if find_verbatim(ts, src) is None:
            n_bad_src += 1
        if find_verbatim(tt, tgt) is None:
            n_bad_tgt += 1

        # ---- 门禁 B：第 8/9 列必须是第 4/5 列的合法形态还原，或与之相同
        #（没做还原 / 校验不过时回落到切片 —— 「回落不丢术语」）。
        # 句中切片现在就在手边，所以判定是 O(1)，**不再需要滑窗去猜切片**。
        fo_is_src = cjk_ratio(src) < cjk_ratio(tgt)
        if dict_form:
            fo_span, fo_dict = (ts, ds) if fo_is_src else (tt, dt)
            if not fo_dict:
                n_empty_dict += 1
            elif fo_dict == fo_span:
                stats["exact"] += 1          # 词条形式 == 句中切片：没做还原
            else:
                err = _dict_form_ok(fo_dict, fo_span, lang, analyzer)
                if err is not None:
                    # ⚠ 豁免：词条形式可能**只差标点**。`normalize_delivery` 会把
                    #   外语侧的撇号 / 外层引号归一化后写进 `term_*_base`（也就是
                    #   这一列），那是我们有意做的，不该判成「非法还原」——
                    #   按同一套归一化再比一次即可。
                    from .extract import normalize_delivery_text
                    if fo_dict == normalize_delivery_text(fo_span):
                        err = None
                if err is None:
                    stats["morph"] += 1      # 合法还原（含只差标点）
                else:
                    stats["neither"] += 1
                    stats["why"].append(f"{path.name} {fo_dict!r}: {err}")

        # ---- 同源核对：xlsx 的第 8/9 列必须等于 terms_raw.json 记的那两列。
        # 「xlsx 与 raw 不同源」比「还原对不对」更严重（陈旧 xlsx / 两次运行混目录）。
        if span_idx is not None:
            key = (path.name, _at(r, i_sid), ts, tt)
            pair = span_idx.get(key)
            if pair is None:
                stats["no_raw"] += 1
            elif dict_form:
                want = pair[0] if fo_is_src else pair[1]
                got = ds if fo_is_src else dt
                if want != got:
                    stats["mismatch"] = stats.get("mismatch", 0) + 1
                    if len(stats["why"]) < 40:
                        stats["why"].append(
                            f"{path.name} 句 {_at(r, i_sid)}: 词条形式与原始行不符"
                            f"（xlsx {got!r} / raw {want!r}）")

        for t in _at(r, i_ty).split(","):
            t = t.strip()
            if t and t not in ALLOWED_TYPES:
                n_bad_type += 1
        if i_note is not None and r[i_note] not in (None, ""):
            n_note += 1
    if n_bad_src:
        problems.append(f"{n_bad_src} 行的 term_src 无法在 src_text 中定位")
    if n_bad_tgt:
        problems.append(f"{n_bad_tgt} 行的 term_tgt 无法在 tgt_text 中定位")
    if n_bad_type:
        problems.append(f"{n_bad_type} 处 types 不在 10 类白名单内")
    if n_note:
        problems.append(f"{n_note} 行 note 非空（口径要求恒空）")
    if n_empty_dict:
        problems.append(f"{n_empty_dict} 行的词条形式列（term_*_dict）为空"
                        f"（那一列应当至少等于句中切片）")
    if stats["neither"]:
        problems.append(f"{stats['neither']} 行的词条形式既不是句中切片、"
                        f"也不是它的合法形态还原")
    if stats.get("mismatch"):
        problems.append(f"{stats['mismatch']} 行的词条形式与 terms_raw.json 不符"
                        f"（xlsx 与 raw 不同源）")
    if stats["no_raw"]:
        # xlsx 有这一行、terms_raw.json 却没有 -> 两者不同源，比「还原对不对」更严重
        problems.append(f"{stats['no_raw']} 行在 terms_raw.json 里找不到对应记录"
                        f"（xlsx 与 raw 不同源，可能是陈旧 xlsx 或两次运行混在一个目录）")
    wb.close()
    return problems, stats


def check_names() -> list[str]:
    """产出文件名的行为断言。前 5 条逐字对照 HTML；后 4 条是与 HTML 的有意差异。"""
    cases = [
        # 与 HTML 逐字一致
        ("zh-es_conf_tech_0005_seg002_align.qc.json", "zh-es_conf_tech_0005_seg002_term.xlsx"),
        ("zh-es_conf_econ_0009_seg002.align.qc.json", "zh-es_conf_econ_0009_seg002.term.xlsx"),
        ("foo.json", "foo_term.xlsx"),
        ("foo.jsonl", "foo_term.xlsx"),
        ("foo.txt", "foo.txt_term.xlsx"),
        # 有意差异：上游把 align 拼错的三种写法，我们纠正拼写（法语语料 7 个文件）
        ("zh-fr_tour_serv_0032_seg001_algin.qc.json", "zh-fr_tour_serv_0032_seg001_term.xlsx"),
        ("zh-fr_tour_serv_0001_seg001_aglin.qc.json", "zh-fr_tour_serv_0001_seg001_term.xlsx"),
        ("zh-fr_tour_muse_0001_seg001_align.qc..json", "zh-fr_tour_muse_0001_seg001_term.xlsx"),
        # 但 `_align.json`（align 拼对了、只是缺 .qc）不动，照 HTML 规则走
        ("zh-fr_tour_attr_0020_seg001_align.json", "zh-fr_tour_attr_0020_seg001_align_term.xlsx"),
    ]
    bad = []
    for src, want in cases:
        got, _ = out_name_for(src)
        if got != want:
            bad.append(f"replace_out_name({src!r}) = {got!r}，期望 {want!r}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.verify", description="交付口径校验")
    ap.add_argument("out_dir", nargs="?", default="out/es")
    # ⚠ 必须限定取值。以前是自由字符串，跑俄语忘了写 --lang ru 时默认落到 es，
    #   而 es 也在 DICT_FORM_LANGS 里 —— 于是拿 simplemma 的罗曼语词元器去校验俄语，
    #   结果是一大堆假的「两者都不是」，而不是干脆报错。
    ap.add_argument("--lang", default="es", choices=["es", "fr", "ru", "ja"],
                    help="决定词典形校验用哪个形态分析器；写错会静默出假问题，所以限定取值")
    ap.add_argument("--expect-files", type=int, default=0,
                    help="预期交付 xlsx 个数（取 report 里的「产出 xlsx」）；不符就报问题")
    ap.add_argument("--max-neither", type=float, default=0.0,
                    help="允许的「两者都不是」比例上限，默认 0")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    out_dir = Path(a.out_dir)
    print(f"校验目录: {out_dir}   语种: {a.lang}")
    if a.lang in DICT_FORM_LANGS:
        print(f"形态分析器: {morph_status(a.lang)}")

    span_idx, span_note = load_span_index(out_dir)
    print(f"原句切片索引: {span_note}")

    name_problems = check_names()
    print("\n[文件名规则] " + ("OK" if not name_problems else "有问题"))
    for p in name_problems:
        print("   ", p)

    xs = sorted(out_dir.glob("*.xlsx"))
    # 只认符合 HTML 命名规则的交付文件。`out_name_for()` 的产物一律以 `term.xlsx`
    # 结尾（`_term.xlsx` 或 `.term.xlsx`），所以这个过滤不会漏掉真交付物。
    # ⚠ 踩过：QC 工作簿写进同一个目录，被当成交付文件读，直接 KeyError 崩掉。
    skipped = [p for p in xs if not p.name.endswith("term.xlsx")]
    xs = [p for p in xs if p.name.endswith("term.xlsx")]
    print(f"\n[xlsx] 共 {len(xs)} 个")
    for p in skipped:
        print(f"    (跳过非交付文件 {p.name})")
    # ---- (a) 0 个 xlsx 必须报错。以前 xs 为空时循环不执行，total_problems 只剩
    #      空的 name_problems，于是打印「=== 全部通过 ===」并返回 0 ——
    #      指错目录、或 run 整体失败没写出任何文件，门禁都会给绿灯。
    structural: list[str] = list(name_problems)
    if not xs:
        structural.append(
            f"{out_dir} 里没有任何 *term.xlsx —— 目录写错了，或者 run 没产出交付物。"
            "这一条以前会被当成「全部通过」")
    if a.expect_files and len(xs) != a.expect_files:
        structural.append(f"xlsx 个数 {len(xs)} != 预期 {a.expect_files}"
                          f"（预期值取 report 的「产出 xlsx」）")

    total_problems = structural
    agg = {"exact": 0, "morph": 0, "neither": 0, "why": [], "no_raw": 0}
    for x in xs:
        probs, st = check_xlsx(x, lang=a.lang, span_idx=span_idx or None)
        status = "OK" if not probs else f"{len(probs)} 个问题"
        print(f"    {x.name}: {status}")
        for p in probs:
            print(f"        - {p}")
        total_problems.extend(probs)
        for k in ("exact", "morph", "neither", "no_raw"):
            agg[k] += st[k]
        agg["why"].extend(st["why"])

    if a.lang in DICT_FORM_LANGS and (agg["exact"] or agg["morph"] or agg["neither"]):
        n = agg["exact"] + agg["morph"] + agg["neither"]
        print(f"\n[外语术语列] 共 {n} 行")
        print(f"    精确命中原句切片 : {agg['exact']}  ({agg['exact'] / n:.1%})"
              f"   —— 本来就是词典形，或还原后与句中形式相同")
        print(f"    合法词典形还原   : {agg['morph']}  ({agg['morph'] / n:.1%})")
        print(f"    两者都不是       : {agg['neither']}  ({agg['neither'] / n:.1%})"
              f"   ← 只有这一档是真问题")
        for w in agg["why"][:15]:
            print(f"        - {w}")
        if len(agg["why"]) > 15:
            print(f"        …… 另有 {len(agg['why']) - 15} 条")

    zp = out_dir / DEFAULT_EXPORT.zip_name
    if zp.exists():
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
        flat = all("/" not in n for n in names)
        print(f"\n[zip] {zp.name}  {len(names)} 个成员  扁平={flat}")
        if not flat:
            total_problems.append("zip 内含子目录，应扁平放 basename")

    # ---- (b) 分开裁决。以前 neither>0 就计入 total_problems，
    #      而全量几乎必然有若干条第 3 档 -> 退出码恒为 1，当门禁根本用不了。
    #      现在：结构问题（sheet/列序/中文侧逐字/types/note/文件名/个数/与 raw 同源）
    #      必须为 0；还原比例单独按 --max-neither 裁决。
    n_rows = agg["exact"] + agg["morph"] + agg["neither"]
    neither_rate = agg["neither"] / n_rows if n_rows else 0.0
    hard = [q for q in total_problems
            if "既不是原句切片" not in q]
    print()
    print("=" * 60)
    print(f"结构问题（必须为 0）      : {len(hard)}")
    print(f"「两者都不是」比例        : {agg['neither']}/{n_rows} = {neither_rate:.3%}"
          f"   上限 {a.max_neither:.3%}")
    over = neither_rate > a.max_neither
    if not hard and not over:
        print("=== 全部通过（可交付）===")
        return 0
    for q in hard:
        print("  结构问题:", q)
    if over:
        print(f"  还原比例超限: {neither_rate:.3%} > {a.max_neither:.3%}")
    print("=== 未通过 ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
