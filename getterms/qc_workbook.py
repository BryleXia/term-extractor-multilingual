"""term_qc 环节的人工核对工作簿（**不进交付 zip**）。

为什么要单独一份：交付的 xlsx 必须与 `GetTerms Tool.html` 逐列一致（列名/列序/
sheet 名/`note` 恒空），所以「原句切片」这一列没地方放。但三语的外语侧交的都是
词典形（ISO 10241-1 / IATE 口径），校对人需要能一眼看到
「词典形 ← 句中形式 ← 全句」三者的对应，否则没法判还原对不对。

数据来源：`getterms.run` 落盘的 `out/<lang>/terms_raw.json`，不重新调 API。

用法：
  python -m getterms.qc_workbook out/es
  python -m getterms.qc_workbook out/ru --only-changed      # 只列还原过的行
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import utf8_stdout

# 与 small_sample 同样的理由：不显式指定西文字体，西里尔字母会落到宋体、
# 被渲染成全角（`П р о д в и ж е н и е`），校对人根本没法读。
BODY_FONT = "Calibri"
HDR = Font(bold=True, size=11, name=BODY_FONT)
BODY = Font(size=11, name=BODY_FONT)
FILL_HDR = PatternFill("solid", fgColor="DDEBF7")
FILL_ASK = PatternFill("solid", fgColor="E8F5E9")
FILL_DIFF = PatternFill("solid", fgColor="FFF4E5")   # 还原过的行，浅橙
WRAP = Alignment(wrap_text=True, vertical="top")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)

HEAD = ["文件", "sent_id", "词典形（交付值）", "原句切片（句中形式）", "已还原",
        "类型", "全句 · 该侧", "全句 · 另一侧", "还原是否正确", "备注"]


def load_rows(out_dir: Path) -> tuple[dict, list]:
    p = out_dir / "terms_raw.json"
    if not p.exists():
        raise SystemExit(
            f"找不到 {p}。它由 getterms.run 落盘（除非加了 --no-raw-dump）；"
            "bake-off 的结果在 bakeoff/terms_*.json，不是这个格式。")
    d = json.loads(p.read_text(encoding="utf-8"))
    return d, d.get("rows", [])


def _row_view(r: dict) -> dict:
    """把一条 raw 行摊成核对表要的字段。

    抽成函数是为了让**抽样与写表用同一套判断** —— 否则抽样按一套逻辑挑行、
    写表按另一套算 `changed`，配额就会算错。
    """
    if r.get("term_src_base"):
        side, deliv = "src", r["term_src_base"]
        span = r.get("term_src_span") or r["term_src"]
        other, same = r.get("tgt_text", ""), r.get("src_text", "")
    elif r.get("term_tgt_base"):
        side, deliv = "tgt", r["term_tgt_base"]
        span = r.get("term_tgt_span") or r["term_tgt"]
        other, same = r.get("src_text", ""), r.get("tgt_text", "")
    else:
        side, deliv, span = "-", "", ""
        other = same = ""
    changed = bool(deliv) and deliv != span
    if side == "-":
        # 回落行：交付值就是句中形式
        deliv = span = r.get("term_src") or ""
        same, other = r.get("src_text", ""), r.get("tgt_text", "")
    return {"side": side, "deliv": deliv, "span": span, "changed": changed,
            "same": same, "other": other}


def stratify(rows: list, n: int) -> tuple[list, dict]:
    """按 (层 × 是否还原) 分层的**确定性**抽样，返回 (抽中的行, 说明)。

    两条取舍：
      * **回落行全部保留** —— 那是校验器自己标出来的疑点，一条都不能漏，
        而且很少（实测 西 0.00% / 法 0.00% / 俄 0.32%）。
      * 每层内**等距取**而不是随机取：同一份 terms_raw.json 每次抽出同一批行，
        老师第二次打开看到的还是那些条目，能接着上次判。
    """
    keep, pool = [], collections.defaultdict(list)
    for r in rows:
        v = _row_view(r)
        if v["side"] == "-":
            keep.append(r)
        else:
            pool[(r.get("layer", "?"), v["changed"])].append(r)
    out = list(keep)
    budget = max(0, n - len(keep))
    note = {"回落行（全留）": len(keep)}
    if pool and budget > 0:
        per = max(1, budget // len(pool))
        for k in sorted(pool):
            grp = pool[k]
            take = min(per, len(grp))
            if take >= len(grp):
                out.extend(grp)
            else:
                step = len(grp) / take
                out.extend(grp[int(i * step)] for i in range(take))
            note[f"{k[0]} {'已还原' if k[1] else '未变'}"] = f"{take}/{len(grp)}"
    return out, note


def build(meta: dict, rows: list, out: Path, only_changed: bool = False,
          sampled_from: int = 0) -> tuple[Path, dict]:
    wb = Workbook()
    ws = wb.active
    ws.title = "词典形核对"

    def banner(text: str, *, bold=False, size=11, height=None):
        ws.append([text])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEAD))
        c = ws.cell(r, 1)
        c.font = Font(bold=bold, size=size, name=BODY_FONT)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[r].height = height or (size + 6)

    banner("术语词典形核对（term_qc 用；**此表不进交付 zip**）",
           bold=True, size=14, height=24)
    banner(f"语种 {meta.get('lang')} ｜ 模型 {meta.get('model')} "
           f"{meta.get('effort') or '(默认)'} ｜ 提示词 {meta.get('prompt')} "
           f"(hash {meta.get('phash')})")
    banner("交付给下游工具方的 xlsx 里只有「词典形」这一列；本表把它与原句切片、全句并排，"
           "供人工判断还原是否正确。「已还原」= 是，说明交付值与句中形式不同。")
    if sampled_from:
        banner(f"⚠ 本表是**分层抽样**：从全量 {sampled_from:,} 条里按「层 × 是否还原」"
               f"等距抽出，校验器标出的回落行全部保留。全量原始数据在 terms_raw.json。"
               f"等距抽样是确定性的 —— 重新生成会得到同一批行，可以接着上次判。")
    ws.append([])

    ws.append(HEAD)
    hr = ws.max_row
    for c in range(1, len(HEAD) + 1):
        ws.cell(hr, c).font = HDR
        ws.cell(hr, c).fill = FILL_HDR
        ws.cell(hr, c).border = BOX
    for c in (9, 10):
        ws.cell(hr, c).fill = FILL_ASK

    stat = collections.Counter()
    for r in rows:
        v = _row_view(r)
        side, deliv, span, changed = v["side"], v["deliv"], v["span"], v["changed"]
        same_side_text, other = v["same"], v["other"]
        if side == "-":
            if only_changed:
                stat["跳过（未还原）"] += 1
                continue
        elif only_changed and not changed:
            stat["跳过（还原后与句中形式相同）"] += 1
            continue
        stat["列出"] += 1
        stat["其中已还原" if changed else "其中与句中形式相同"] += 1
        ws.append([r.get("file", ""), r.get("sent_id", ""), deliv, span,
                   "是" if changed else "", r.get("types", ""),
                   same_side_text, other, "", ""])
        row = ws.max_row
        for c in range(1, len(HEAD) + 1):
            ws.cell(row, c).border = BOX
            ws.cell(row, c).alignment = WRAP
            ws.cell(row, c).font = BODY
            if changed and c in (3, 4, 5):
                ws.cell(row, c).fill = FILL_DIFF
        for c in (9, 10):
            ws.cell(row, c).fill = FILL_ASK

    widths = {1: 34, 2: 8, 3: 26, 4: 26, 5: 7, 6: 14, 7: 48, 8: 48, 9: 14, 10: 20}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = f"A{hr + 1}"

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".tmp{os.getpid()}")
    try:
        wb.save(tmp)
        os.replace(tmp, out)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return out, dict(stat)


def main(argv=None) -> int:
    # ⚠ 在建 parser 之前切 UTF-8：`--help` 的中文由 argparse 直接打印，
    #   走控制台默认编码（中文 Windows 是 GBK）会 UnicodeEncodeError。
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="getterms.qc_workbook",
                                 description="term_qc 词典形核对工作簿")
    ap.add_argument("out_dir", nargs="?", default="out/es")
    ap.add_argument("--only-changed", action="store_true",
                    help="只列真的做了还原的行（交付值与句中形式不同）")
    ap.add_argument("-o", "--output", default=None,
                    help="输出路径，默认 <out_dir>/qc_词典形核对.xlsx")
    ap.add_argument("--sample", type=int, default=1200,
                    help="分层抽样的目标行数，默认 1200（这张表是给人逐条看的）")
    ap.add_argument("--all", action="store_true",
                    help="不抽样，列出全部行。⚠ 全量 8 万+ 行会吃掉数 GB 内存且极慢")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    out_dir = Path(a.out_dir)
    meta, rows = load_rows(out_dir)
    # 默认写进 `_qc/` 子目录，**不与交付 xlsx 同级**：
    #   * `pack_zip` 只打包它自己写出的文件，本来就不会收它；
    #   * 但 `verify` 会 glob 交付目录下的 `*.xlsx`，同级放会被当交付文件读（踩过）；
    #   * 人工整目录拷给下游工具方时也不会顺手把它带过去。
    dest = Path(a.output) if a.output else out_dir / "_qc" / "qc_词典形核对.xlsx"

    total = len(rows)
    sampled_from = 0
    if a.all:
        if total > 5000:
            print(f"⚠ --all 且有 {total:,} 行：openpyxl 要建 {total * len(HEAD):,} 个"
                  "单元格，可能吃掉数 GB 内存并跑很久。")
    elif total > a.sample:
        rows, note = stratify(rows, a.sample)
        sampled_from = total
        print(f"分层抽样 {total:,} -> {len(rows):,} 行")
        for k, v in note.items():
            print(f"    {k}: {v}")

    p, stat = build(meta, rows, dest, only_changed=a.only_changed,
                    sampled_from=sampled_from)
    print(f"本表行数 {len(rows)} 条（全量 {total} 条在 terms_raw.json）")
    for k, v in stat.items():
        print(f"  {k}: {v}")
    print(f"写出: {p}")
    print("⚠ 此表**不要**放进交付 zip —— 交付 xlsx 必须与 HTML 工具逐列一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
