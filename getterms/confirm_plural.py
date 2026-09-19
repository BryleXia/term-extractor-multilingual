"""复数判据的**内部对照表**（三语各一份 xlsx + md）。

⚠ **这张表不再交给老师。** 2026-09-18 它列出的 23 条待问事项逐条联网查证后，
  **0 条真的需要母语老师**：17 条词典/术语库/法律文本直接能答（DLE 的 `m. pl.` /
  `U. m. en pl.` 标注、IATE 词条形与条目号、法兰西学院词典、魁北克 GDT、
  俄罗斯家庭法典第 153 条条标题…），4 条读一眼原句就能判，2 条是规则边界
  （魁北克 BDL《Nombre du complément du nom》与 Le Robert 有明文）。
  查证结论已写进 `plural_lexicon.py` 每条的 `source` 字段。
  老师该做的是**验收小样**（`small_sample.py`，3~5 分钟），不是替我查词典。

  查证还查出这张表本身有三处是我们自己的错，正好说明它不该直接外发：
    · `sources d'eau` 是**匹配假阳性**（`lookup()` 只比中心词，撞上了 `sources de Baotu`）；
    · `sources de Baotu` 清单与表**自相矛盾**（C 组说惯用复数、B 组交单数，
      而查实趵突泉是一处专名泉、复数形精确检索 0 命中）；
    · `flores y pájaros` 的理由写「作画科名称时」，可原句是普通并列宾语枚举。

它现在的用途：改动前后的**内部逐条对照**，看清单命中与多臂一致性。
表里的分组沿用原设计：
  问题一：这些术语现在**保留复数**了（原来压成单数）；
  问题二：仍压成单数、但命中了清单的（修完清单后应为 0）；
  问题三：清单本身（`plural_lexicon.py`）。

用法：
  python -m getterms.confirm_plural --lang fr      # 内部对照，不外发
  python -m getterms.confirm_plural --all

表格规矩沿用已被三位老师用过的那套：单 sheet、问题放顶部、**不冻结**、
Calibri（否则法语音符字母与西里尔字母落到宋体会变全角）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import FINAL_DUMPS, OUT_DIR, utf8_stdout
from .extract import load_lemmatizer, load_morph
from .plural_lexicon import HABITUAL_PLURAL
from .selfcheck import check_plural_dropped, compare, load_rows

# 各语种的新版探针（同一提示词的多次采样）。只认多臂一致的变化，见 selfcheck.compare。
# ⚠ 一律用 `__g2` 那一批：它们是**修好俄语一格闸门之后**从缓存重放的结果
#   （$0），也就是我们真正会交付的值。老师不该看旧闸门下的回落产物。
ARMS: dict[str, tuple[str, ...]] = {
    # ⚠ 西语这两个臂以前写的是 `__g2` / `__g2__r2`，**两个文件都不存在**
    #   （实测 exists()=False）；现役是 `__g3`。第五次「工具默认值指向别的版本」。
    "es": ("terms_gemini-3_7-flash__high__b10__es_v8__g3.json",
           "terms_gemini-3_7-flash__high__b10__es_v8__g3__r2.json"),
    "fr": ("terms_gemini-3_7-flash__high__b10__fr__fr_v7__g2.json",
           "terms_gemini-3_7-flash__high__b10__fr__fr_v7__g2__r2.json"),
    "ru": ("terms_gemini-3_7-flash__high__b10__ru__pf35__ru_v6__full_g2.json",
           "terms_gemini-3_7-flash__high__b10__ru__pf35__ru_v6__full_g2__r2.json"),
}
# 基线也要同闸门重算，否则「改动前后」里混着闸门差异。
# 俄语另外还必须用**同一份全量语料样本**上的 ru_v5 —— 已批准的那份出自测试语料，
# 只覆盖 4 层，拿它比会把语料差异算成版本差异。
BASE: dict[str, str] = {
    "es": "terms_gemini-3_7-flash__high__b10__es_v6.json",
    "fr": "terms_gemini-3_7-flash__high__b10__fr__fr_v6.json",
    "ru": "terms_gemini-3_7-flash__high__b10__ru__pf35__ru_v5__full_g2.json",
}
LANG_NAME = {"es": "西语", "fr": "法语", "ru": "俄语"}

# 语种专属的追加问题：(问题文本, 要列出的 span 清单)。
# D 组列的是**补语的数**被规范过的条目 —— 这是法语老师 ②（表类别 -> 单数）与
# ③（由多个实体构成 -> 保留复数）的边界，程序划不了，只有她能划。
EXTRA_Q: dict[str, tuple[str, tuple[str, ...]]] = {
    "fr": (
        "问题四（D 组）：同一个中文「面食」，法语在两句里分别写成 "
        "aliments à base de farine（单数）与 aliments à base de farines（复数）。"
        "我们按您的 ② 把后者也规范成 aliment à base de farine。"
        "如果这里的 farines 指「多种面粉」，是不是该按您的 ③ 保留复数？",
        ("aliments à base de farines", "aliments à base de farine"),
    ),
}
VERSION = {"es": ("es_v6", "es_v8"), "fr": ("fr_v6", "fr_v7"), "ru": ("ru_v5", "ru_v6")}

FONT = "Calibri"
HDR = Font(bold=True, size=11, name=FONT)
BODY = Font(size=11, name=FONT)
BOLD = Font(bold=True, size=11, name=FONT)
F_HDR = PatternFill("solid", fgColor="DDEBF7")
F_ASK = PatternFill("solid", fgColor="E8F5E9")
F_Q = PatternFill("solid", fgColor="FFE0B2")
WRAP = Alignment(wrap_text=True, vertical="top")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
NCOL = 6


def _bakeoff(name: str) -> Path:
    from .config import BAKEOFF_DIR
    return BAKEOFF_DIR / name


def collect(lang: str) -> dict:
    """算出三组内容：修好了 / 仍拿不准 / 清单现状。"""
    base_p = _bakeoff(BASE[lang]) if lang in BASE else Path(FINAL_DUMPS[lang])
    _ = FINAL_DUMPS  # 保留引用：定稿 dump 的登记处仍是 config
    arm_ps = [_bakeoff(n) for n in ARMS[lang]]
    missing = [p.name for p in [base_p, *arm_ps] if not p.exists()]
    if missing:
        raise SystemExit(f"{lang}: 缺这些 dump，先跑探针：{missing}")
    analyzer = load_morph() if lang == "ru" else load_lemmatizer()
    base_rows, _n = load_rows(base_p)
    arms = [load_rows(p)[0] for p in arm_ps]
    cmp_ = compare(base_rows, arms, lang, analyzer)
    # 新版里仍命中清单却被压成单数的 —— 两臂都命中才算（单臂命中是抖动）
    tiers = [check_plural_dropped(rs, lang, analyzer) for rs in arms]
    keys = [{d["span"] for d in t["tier1"]} for t in tiers]
    both = set.intersection(*keys) if keys else set()
    still = {}
    for t in tiers:
        for d in t["tier1"]:
            if d["span"] in both:
                still.setdefault(d["span"], d)
    # 原句：从新臂里找
    sent = {}
    for rs in arms:
        for r in rs:
            sent.setdefault(r.span, (r.fo_text, r.zh_text, r.zh_term))
    # 新版的交付值（两臂一致才收 —— 不一致的是抖动，不该拿去占老师的时间）
    deliver: dict[str, str] = {}
    for span in {r.span for rs in arms for r in rs if r.span}:
        vals = {r.base for rs in arms for r in rs if r.span == span and r.base}
        if len(vals) == 1:
            deliver[span] = vals.pop()
    return {"fixed": cmp_["fixed"], "still": sorted(still.values(), key=lambda d: d["span"]),
            "over": cmp_["over"], "jitter": cmp_["jitter"], "sent": sent,
            "deliver": deliver,
            "base": base_p.name, "arms": [p.name for p in arm_ps]}


def build(lang: str, data: dict) -> tuple[Path, Path]:
    old_v, new_v = VERSION[lang]
    wb = Workbook()
    ws = wb.active
    ws.title = "口径确认"

    def banner(text, *, bold=False, size=11, height=None, fill=None):
        ws.append([text])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOL)
        c = ws.cell(r, 1)
        c.font = Font(bold=bold, size=size, name=FONT)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        if fill:
            c.fill = fill
        ws.row_dimensions[r].height = height or (size + 8)

    banner(f"{LANG_NAME[lang]}术语抽取 · 单复数口径确认（请老师过目）",
           bold=True, size=14, height=24)
    banner("我们自查时发现指令里有一句写窄了，已经改好，请您确认改得对不对。约 3 分钟。")
    banner("国际术语规范（ISO 10241-1、欧盟 IATE 手册）的原话是：名词用单数，"
           "**除非该术语习惯上以复数使用**。而我们原来的指令写成了「只以复数存在的才保留复数」—— "
           "这比原话严得多：precipitación、objectif、осадок 的单数在语法上都存在，"
           "于是模型把「大气降水」「可持续发展目标」这类本该用复数的术语压成了单数。",
           fill=F_ASK)
    banner("除这一处，**其它口径一条没动**：抽哪些词、类型怎么标、中文侧怎么处理、"
           "以及您上次提出的修改，全部保持原样。")
    banner(f"问题一（A 组，{len(data['fixed'])} 条）：这些原来被压成了单数，"
           "现在**保留复数**。请扫一眼有没有其实该用单数的。"
           "都对就在 A 组第一行写「都对」，不必逐条填。", fill=F_Q)
    if data["still"]:
        banner(f"问题二（B 组，{len(data['still'])} 条）：这几条我们**仍然压成了单数**，"
               "但我们拿不准，请您定：该保留复数，还是单数是对的？", fill=F_Q)
    banner(f"问题三：请补充**{LANG_NAME[lang]}里习惯用复数的术语**（表末 C 组列了我们现有的"
           f"{len(HABITUAL_PLURAL[lang])} 条）。这是词汇知识，程序推不出来；"
           "您补的词我们直接加进清单，**不用重跑、不改指令**。", fill=F_Q)
    if data["over"]:
        banner(f"问题五（E 组，{len(data['over'])} 条）：这几条我们也改成了**保留复数**，"
               "但它们不在我们那份「惯用复数」清单里 —— 我们判不准，"
               "请您定：保留复数对，还是原来的单数对？", fill=F_Q)
    extra = EXTRA_Q.get(lang)
    if extra and any(s in data["deliver"] for s in extra[1]):
        banner(extra[0], fill=F_Q)
    ws.append([])

    HEAD = ["组", f"原句（{LANG_NAME[lang]}）", "中文", "句中形式", "我们交付的（词典形）",
            "您的判断"]
    ws.append(HEAD)
    hr = ws.max_row
    for c in range(1, NCOL + 1):
        ws.cell(hr, c).font = HDR
        ws.cell(hr, c).fill = F_HDR
        ws.cell(hr, c).border = BOX
    ws.cell(hr, NCOL).fill = F_ASK

    def row(group, sent, zh, span, deliver):
        ws.append([group, sent, zh, span, deliver, ""])
        r = ws.max_row
        for c in range(1, NCOL + 1):
            ws.cell(r, c).border = BOX
            ws.cell(r, c).alignment = WRAP
            ws.cell(r, c).font = BODY
        ws.cell(r, 5).font = BOLD
        ws.cell(r, NCOL).fill = F_ASK

    for d in data["fixed"]:
        fo, zh_t, zh_term = data["sent"].get(d["span"], ("", "", ""))
        row("A", fo, zh_term or zh_t, d["span"], d["new"])
    for d in data["still"]:
        fo, _zh_t, zh_term = data["sent"].get(d["span"], ("", "", ""))
        row("B", fo, zh_term or d.get("zh", ""), d["span"], d["base"])
    for d in data["over"]:
        fo, _zh_t, zh_term = data["sent"].get(d["span"], ("", "", ""))
        row("E", fo, zh_term, d["span"], d["new"])
    if extra:
        for span in extra[1]:
            if span in data["deliver"]:
                fo, _zh_t, zh_term = data["sent"].get(span, ("", "", ""))
                row("D", fo, zh_term, span, data["deliver"][span])
    for e in HABITUAL_PLURAL[lang]:
        row("C（现有清单，供参考/补充）", e.why, e.zh, "", e.term)

    for c, w in {1: 22, 2: 58, 3: 30, 4: 26, 5: 30, 6: 18}.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = None
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(NCOL)}{ws.max_row}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    xlsx = OUT_DIR / f"单复数口径确认_{LANG_NAME[lang]}_请老师过目.xlsx"
    tmp = xlsx.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    import os
    os.replace(tmp, xlsx)

    L = [f"# {LANG_NAME[lang]}术语抽取 · 单复数口径确认", "",
         f"指令版本 {old_v} -> {new_v}；基线 `{data['base']}`；"
         f"新版探针 {len(data['arms'])} 次采样（只认两次一致的变化）。", "",
         "国际术语规范的原话是「名词用单数，**除非该术语习惯上以复数使用**」，"
         "而我们原来的指令写成了「只以复数存在的才保留复数」，比原话严得多。已改回原话。", "",
         f"## 问题一：现在保留复数（{len(data['fixed'])} 条）", "",
         "| 句中形式 | 我们交付的 | 为什么判为惯用复数 |", "|---|---|---|"]
    for d in data["fixed"]:
        L.append(f"| `{d['span']}` | **`{d['new']}`** | {d.get('why', '')} |")
    if data["still"]:
        L += ["", f"## 问题二：仍压成单数、我们拿不准（{len(data['still'])} 条）", "",
              "| 句中形式 | 我们交付的 | 中文 |", "|---|---|---|"]
        for d in data["still"]:
            L.append(f"| `{d['span']}` | **`{d['base']}`** | {d.get('zh', '')} |")
    if data["over"]:
        L += ["", f"## 问题五：改成保留复数、但不在清单里（{len(data['over'])} 条）", "",
              "我们判不准，请您定。", "",
              "| 句中形式 | 原来交付的 | 现在交付的 |", "|---|---|---|"]
        for d in data["over"]:
            L.append(f"| `{d['span']}` | `{d['old']}` | **`{d['new']}`** |")
    if extra and any(s in data["deliver"] for s in extra[1]):
        L += ["", "## 问题四：补语的数（您的 ②/③ 边界）", "", extra[0], "",
              "| 句中形式 | 我们交付的 |", "|---|---|"]
        for span in extra[1]:
            if span in data["deliver"]:
                L.append(f"| `{span}` | **`{data['deliver'][span]}`** |")
    L += ["", f"## 问题三：现有「惯用复数」清单（{len(HABITUAL_PLURAL[lang])} 条），请补充", "",
          "| 术语 | 中文 | 为什么 |", "|---|---|---|"]
    for e in HABITUAL_PLURAL[lang]:
        L.append(f"| `{e.term}` | {e.zh} | {e.why} |")
    md = xlsx.with_suffix(".md")
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    return xlsx, md


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getterms.confirm_plural")
    ap.add_argument("--lang", choices=["es", "fr", "ru"], default=None)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdout()
    langs = ["es", "fr", "ru"] if a.all or not a.lang else [a.lang]
    for lang in langs:
        data = collect(lang)
        xlsx, md = build(lang, data)
        print(f"[{LANG_NAME[lang]}] A 组（现在保留复数）{len(data['fixed'])} 条；"
              f"B 组（仍拿不准）{len(data['still'])} 条；"
              f"过度纠正 {len(data['over'])} 条；抖动 {len(data['jitter'])} 条")
        print(f"  {xlsx}")
        print(f"  {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
