"""生成给语言老师的人工审阅表（计划 §4.4 / 阶段 3 要求）。

读 bakeoff/terms_*.json，产出一个 xlsx：
  sheet 汇总       —— 各参赛组合的指标并排
  sheet 逐句对照   —— 每句原文 + 每个模型抽到的术语，并排
  sheet 分歧       —— 只有部分模型抽到的术语（最值得人看的部分）
  sheet 一致       —— 所有模型都抽到的术语（可快速批量确认）

用法：
  python -m getterms.bakeoff_review --lang es
  python -m getterms.bakeoff_review --lang ru            # 俄语的 label 带 __pf，见下
  python -m getterms.bakeoff_review --labels a b         # 只比这两个 label

⚠ 2026-09-18 修掉的两个 bug（这份表生成于只有西语的 9-16，之后一直没人再跑过）：

1. `sample="default"` 用「label 里没有 `__pf`」来认默认样本，而**俄语全部 label 都带
   `__pf35`** —— 俄语被整语种静默排除，表里一条都没有。现在先按 `--lang` 选语种，
   `__pf` 过滤只在同语种内部生效，且**筛完为空就报错并列出可用 label**，不交空表。

2. 「一致」sheet 的判据原来是 `len(hits) == len(labels)`，拿**全部 label 数**（当时 22 个，
   跨语种跨样本）当分母 —— 没有术语能被 22 个 label 同时抽到，「一致」表**恒为空**，
   「分歧」表则把所有东西都收了。分母现在改成「**实际处理过这一句**的 label 数」，
   并要求 ≥2 个 label 覆盖才进「一致」（只有一个 label 时「一致」没有意义）。

⚠ 一张表只放一个语种。跨语种 label 并排会让「分歧/一致」两个 sheet 失去意义
  （不同语种本来就抽不到彼此的术语），所以检测到跨语种直接报错。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import BAKEOFF_DIR, utf8_stdout
from .quality import analyse


def lang_of(label: str) -> str:
    """从 label 反推语种。约定：出现 `__fr` / `__ru` 即该语种，都没有就是西语。"""
    return "ru" if "__ru" in label else ("fr" if "__fr" in label else "es")


def _keep(label: str, lang: str | None, sample: str) -> bool:
    if lang and lang_of(label) != lang:
        return False
    if sample == "all":
        return True
    if sample == "default":
        # ⚠ 只在**同语种内部**判「默认样本」。俄语的默认样本本身就叫 __pf35，
        #   用全局的「没有 __pf」去筛会把俄语整个筛掉（原 bug）。
        return lang_of(label) == "ru" or "__pf" not in label
    return sample in label


def load_runs(sample: str = "default", lang: str | None = None,
              labels: list[str] | None = None) -> dict[str, list[dict]]:
    """只收同一份样本、同一个语种的条目 —— 混进一张表就不可比了。"""
    runs: dict[str, list[dict]] = {}
    for p in sorted(BAKEOFF_DIR.glob("terms_*.json")):
        label = p.stem[len("terms_"):]
        if labels is not None:
            if label not in labels:
                continue
        elif not _keep(label, lang, sample):
            continue
        runs[label] = json.loads(p.read_text(encoding="utf-8"))
    return runs


def load_reports(sample: str = "default", lang: str | None = None,
                 labels: list[str] | None = None) -> dict[str, dict]:
    """标签**直接取自文件名**，不要从报告字段反推。

    反推会出错：pf10 与 pf30 两个样本的报告，model/effort/batch_size 完全相同，
    推出的标签也相同 → 后读的覆盖先读的，汇总页就会把另一个样本的成本贴到这一行。
    文件名是唯一可靠的身份（report_<label>.json 与 terms_<label>.json 一一对应）。
    """
    reps: dict[str, dict] = {}
    for p in sorted(BAKEOFF_DIR.glob("report_*.json")):
        label = p.stem[len("report_"):]
        if labels is not None:
            if label not in labels:
                continue
        elif not _keep(label, lang, sample):
            continue
        reps[label] = json.loads(p.read_text(encoding="utf-8"))
    return reps


def _autosize(ws, widths: dict[int, int]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


HDR = Font(bold=True)
FILL_HDR = PatternFill("solid", fgColor="DDEBF7")
FILL_DIFF = PatternFill("solid", fgColor="FFF2CC")
WRAP = Alignment(wrap_text=True, vertical="top")


def build(out_path: Path, sample: str = "default", lang: str | None = None,
          only: list[str] | None = None) -> Path:
    runs = load_runs(sample, lang, only)
    reps = load_reports(sample, lang, only)
    if not runs:
        avail = "\n  ".join(p.stem[len("terms_"):]
                            for p in sorted(BAKEOFF_DIR.glob("terms_*.json")))
        raise SystemExit(
            f"筛完一个 label 都不剩（lang={lang} sample={sample} labels={only}）。"
            f"\n{BAKEOFF_DIR} 里现有的 label：\n  {avail}")
    langs = {lang_of(lb) for lb in runs}
    if len(langs) > 1:
        raise SystemExit(
            f"这批 label 跨了多个语种 {sorted(langs)}：{sorted(runs)}。"
            "一张表只放一个语种，否则「分歧/一致」两个 sheet 没有意义 —— 请加 --lang。")

    labels = sorted(runs)
    wb = Workbook()

    # ---------------------------------------------------------- sheet 汇总
    ws = wb.active
    ws.title = "汇总"
    cols = ["参赛（模型/档位/批大小）", "术语数", "密度(个/句)", "JSON成功率",
            "逐字通过率", "拒答", "截断", "思考占输出", "平均延迟s",
            "本轮成本$", "外推全量$",
            "other占比(低=好)", "像从句占比(低=好)", "含逗号(低=好)",
            "other混用(应为0)", "tab总数", "hot总数", "poli层tab占比",
            "硬门槛裁决"]
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        ws.cell(1, c).font = HDR
        ws.cell(1, c).fill = FILL_HDR
    for lb in labels:
        d = reps.get(lb, {})
        der = d.get("derived", {})
        q = analyse(runs[lb])
        think = (d.get("reasoning_tokens", 0) / d["completion_tokens"]
                 if d.get("completion_tokens") else 0)
        lat = (d.get("latency_sum", 0) / d["calls"]) if d.get("calls") else 0
        ws.append([
            lb, len(runs[lb]), round(der.get("terms_per_sentence", 0), 2),
            round(der.get("json_ok_rate", 0), 4),
            round(der.get("anchor_pass_rate", 0), 4),
            d.get("refused", 0), d.get("truncated", 0), round(think, 4),
            round(lat, 1), round(der.get("cost_usd", 0), 4),
            der.get("extrapolated_es_full", 0),
            round(q.get("other_share", 0), 4),
            round(q.get("clause_share", 0), 4),
            q.get("punct_in_term", 0),
            q.get("other_mixed", 0),
            q.get("tab_count", 0),
            q.get("hot_count", 0),
            round(q.get("conf_poli_tab_share", 0), 4),
            "; ".join(der.get("gate_verdict", [])),
        ])
    _autosize(ws, {1: 42, 19: 46})
    ws.freeze_panes = "B2"

    # 表头下方加一行说明，老师不用问怎么读
    ws.append([])
    ws.append(["怎么读：other 占比高 = 认不出领域往 other 倒；像从句/含逗号 = 抽的不是"
               "术语而是短语；poli 层 tab 占比过低 = 敏感词漏标，是安全过滤的信号"])

    # 索引：(file, sent_id) -> 原文；(file, sent_id, term_src, term_tgt) -> {label: types}
    sent_text: dict[tuple, tuple[str, str, str]] = {}
    term_hits: dict[tuple, dict[str, str]] = defaultdict(dict)
    # 哪个 label 处理过哪一句 —— 「一致」的分母只能是**覆盖了这一句**的 label 数
    sent_cov: dict[tuple, set[str]] = defaultdict(set)
    for lb, rows in runs.items():
        for r in rows:
            key = (r["file"], str(r["sent_id"]))
            sent_cov[key].add(lb)
            sent_text[key] = (r["layer"], r["src"], r["tgt"])
            tk = (r["file"], str(r["sent_id"]), r["term_src"], r["term_tgt"])
            term_hits[tk][lb] = r["types"]

    # ------------------------------------------------------ sheet 逐句对照
    ws2 = wb.create_sheet("逐句对照")
    head = ["层", "文件", "sent_id", "src", "tgt"] + [f"{lb}" for lb in labels]
    ws2.append(head)
    for c in range(1, len(head) + 1):
        ws2.cell(1, c).font = HDR
        ws2.cell(1, c).fill = FILL_HDR
    by_sent: dict[tuple, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for (f, sid, ts, tt), hits in term_hits.items():
        for lb, types in hits.items():
            by_sent[(f, sid)][lb].append(f"{ts} = {tt} [{types}]")
    for key in sorted(by_sent, key=lambda k: (sent_text[k][0], k[0], int(k[1]) if k[1].isdigit() else 0)):
        layer, src, tgt = sent_text[key]
        row = [layer, key[0], key[1], src, tgt]
        for lb in labels:
            row.append("\n".join(sorted(by_sent[key].get(lb, []))))
        ws2.append(row)
        for c in range(4, len(head) + 1):
            ws2.cell(ws2.max_row, c).alignment = WRAP
    _autosize(ws2, {1: 11, 2: 40, 3: 8, 4: 46, 5: 46,
                    **{6 + i: 34 for i in range(len(labels))}})
    ws2.freeze_panes = "F2"

    # ---------------------------------------------------------- sheet 分歧 / 一致
    ws3 = wb.create_sheet("分歧")
    ws4 = wb.create_sheet("一致")
    h2 = ["层", "文件", "sent_id", "term_src", "term_tgt", "抽到的模型数"] + labels + ["src", "tgt"]
    for w in (ws3, ws4):
        w.append(h2)
        for c in range(1, len(h2) + 1):
            w.cell(1, c).font = HDR
            w.cell(1, c).fill = FILL_HDR
        w.freeze_panes = "G2"
    n = len(labels)
    for (f, sid, ts, tt), hits in sorted(
            term_hits.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])):
        layer, src, tgt = sent_text[(f, sid)]
        row = [layer, f, sid, ts, tt, len(hits)]
        row += [hits.get(lb, "") for lb in labels]
        row += [src, tgt]
        # ⚠ 分母是**处理过这一句**的 label 数，不是全部 label 数（原 bug 让「一致」恒空）
        cov = sent_cov[(f, sid)]
        target = ws4 if (len(cov) >= 2 and len(hits) == len(cov)) else ws3
        target.append(row)
        if target is ws3:
            target.cell(target.max_row, 6).fill = FILL_DIFF
    for w in (ws3, ws4):
        _autosize(w, {1: 11, 2: 40, 3: 8, 4: 26, 5: 26, 6: 12,
                      **{7 + i: 14 for i in range(n)},
                      6 + n + 1: 46, 6 + n + 2: 46})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.bakeoff_review")
    ap.add_argument("--lang", choices=["es", "fr", "ru"], default=None,
                    help="只收该语种的 label（俄语 label 带 __pf，见模块 docstring）")
    ap.add_argument("--sample", default="default",
                    help="default / all / 或 label 里的样本标记（如 pf35）")
    ap.add_argument("--labels", nargs="*", default=None, help="只比这些 label")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    utf8_stdout()

    name = {"es": "西语", "fr": "法语", "ru": "俄语"}.get(a.lang or "", "多组")
    out = Path(a.out) if a.out else BAKEOFF_DIR / f"语言老师审阅表_{name}bakeoff.xlsx"
    p = build(out, a.sample, a.lang, a.labels)
    runs = load_runs(a.sample, a.lang, a.labels)
    print(f"审阅表已生成: {p}")
    print(f"参赛 {len(runs)} 组: {', '.join(sorted(runs))}")
    tot = {k: len(v) for k, v in runs.items()}
    print(f"各组术语数: {tot}")
    print("\nsheet 说明：")
    print("  汇总      —— 指标并排，含预注册硬门槛裁决")
    print("  逐句对照  —— 每句原文 + 各模型抽到的术语，并排看")
    print("  分歧      —— 只有部分模型抽到的术语（最值得人工判断的部分）")
    print("  一致      —— 所有模型都抽到的（可快速批量确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
