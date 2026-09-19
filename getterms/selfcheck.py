"""三语术语质量自检（零成本，不调 API）。

用法：
  python -m getterms.selfcheck --lang es --source bakeoff/terms_..._es_v6.json
  python -m getterms.selfcheck --all          # 三语各取默认定稿 dump
  python -m getterms.selfcheck --lang es --source out/es/terms_raw.json

设计上的三条硬约束（每条都是今天踩出来的）：

1. **必须显式声明量的是哪一列。** `bakeoff.py` 写 dump 时 `term_src`/`term_tgt` 取的是
   `out_src`/`out_tgt` = 有词典形就用词典形。所以 **v5 及更早的文件里它是原句切片，
   v6 起它是交付值**。`quality.py`/`span_defects.py`/`bakeoff_review.py` 三个都只读这两列、
   从不读 `_span`/`_base`，拿 v6 对 v5 做回归会把两个不同的量画进同一张表而不报警。
   本模块一律用 `term_*_span`（切片）与 `term_*_base`（词典形）两个显式列，
   只在旧 dump 缺列时才回落，并在报告里写明。

2. **两种 dump 字段名都要认**：bake-off 用 `src`/`tgt`，`terms_raw.json` 用
   `src_text`/`tgt_text`。今天已经被这个坑过一次（确认表的原句列静默变空）。

3. **候选清单上线前必须量假阳性率，> 30% 不予采用。** 今天四个临时检测器全军覆没：
   俄语性翻转 3/3 假阳性、俄语从属成分 38/38、敏感词漏标 12/12、错配启发式没地基。
   所以「复数→单数」**分两档**：命中惯用复数清单的才算告警（精确率高），
   其余明确标成「抽样供扩清单，不是错误列表」—— 一个 38/38 喊狼的检测器比没有更糟。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import FINAL_DUMPS, utf8_stdout
from .extract import (DICT_FORM_LANGS, complement_num_changed, head_group,
                      load_lemmatizer, load_morph, morph_status, nom_words)
from .plural_lexicon import lookup as lex_lookup
from .plural_lexicon import lookup_term as lex_lookup_term
from .plural_lexicon import HABITUAL_PLURAL as _HABITUAL

# 各语种的定稿 dump（`--all` 用）。登记在 config.FINAL_DUMPS，
# 与 span_defects 共用同一份，免得两个工具各指一个版本。
DEFAULT_SOURCE = FINAL_DUMPS

# 判「外语侧中心词是复数」用的词尾（es/fr）
_PLURAL_END = ("s", "x")
_FW = {
    "es": {"de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
           "y", "e", "o", "u", "en", "a", "al", "con", "por", "para", "su", "sus"},
    "fr": {"de", "du", "des", "d'", "la", "le", "les", "l'", "un", "une",
           "et", "ou", "en", "à", "au", "aux", "son", "sa", "ses", "ce", "cette"},
}


@dataclass
class Row:
    file: str
    sent_id: object
    layer: str
    zh_term: str
    span: str            # 外语侧**原句切片**
    base: str            # 外语侧**词典形**（空 = 未还原/回落）
    zh_text: str
    fo_text: str
    types: str


def load_rows(path: Path) -> tuple[list[Row], list[str]]:
    """读任意一种 dump，归一成 Row。返回 (rows, 关于数据形态的说明)。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("rows", [])
    notes: list[str] = []
    if not items:
        return [], ["空 dump"]
    has_explicit = "term_src_span" in items[0]
    if not has_explicit:
        notes.append(
            "⚠ 这份 dump 没有 term_*_span / term_*_base 两列（v5 及更早）。"
            "此时 term_src/term_tgt 是**原句切片**，词典形无从得知 —— "
            "「复数→单数」与「跨文件一致性」两项会整体跳过。")
    rows: list[Row] = []
    for r in items:
        fo_src = bool(r.get("term_src_base"))
        if has_explicit:
            span = (r.get("term_src_span") if fo_src else r.get("term_tgt_span")) or ""
            base = (r.get("term_src_base") if fo_src else r.get("term_tgt_base")) or ""
        else:
            span, base = ((r.get("term_src") or "") if fo_src
                          else (r.get("term_tgt") or "")), ""
        # 两种字段名都认
        s_text = r.get("src") or r.get("src_text") or ""
        t_text = r.get("tgt") or r.get("tgt_text") or ""
        zh_term = (r.get("term_tgt") if fo_src else r.get("term_src")) or ""
        if not has_explicit:
            # 无 base 时按汉字占比现判哪边是外语，span 取外语侧
            from .corpus import cjk_ratio
            fo_src = cjk_ratio(s_text) < cjk_ratio(t_text)
            span = (r.get("term_src") if fo_src else r.get("term_tgt")) or ""
            zh_term = (r.get("term_tgt") if fo_src else r.get("term_src")) or ""
        rows.append(Row(
            file=r.get("file", ""), sent_id=r.get("sent_id"),
            layer=r.get("layer", "?"), zh_term=zh_term, span=span, base=base,
            zh_text=(t_text if fo_src else s_text),
            fo_text=(s_text if fo_src else t_text),
            types=r.get("types", "")))
    return rows, notes


# ------------------------------------------------------------ 中心词的数
def _head_es_fr(words: list[str], lang: str) -> str:
    fw = _FW.get(lang, frozenset())
    return next((w for w in words if w.casefold() not in fw), "")


def head_number_dropped(span: str, base: str, lang: str, analyzer) -> str | None:
    """外语侧**中心词**由复数变成了单数 -> 返回中心词的复数形式；否则 None。

    这**不是**错误判定 —— 绝大多数单数化是对的（ISO 10241-1 §6.2.2）。
    它只负责把「动过中心词的数」的条目挑出来，再由惯用复数清单分档。
    """
    if not span or not base or span == base:
        return None
    wa, wb = nom_words(span), nom_words(base)
    if not wa or not wb or len(wa) != len(wb):
        return None
    if lang == "ru":
        if analyzer is None:
            return None
        ha, hb = head_group(wa, analyzer), head_group(wb, analyzer)
        if not ha or not hb:
            return None
        x, y = ha[-1], hb[-1]

        def num(w):
            if not analyzer.word_is_known(w):
                return "?"
            s = {p.tag.number for p in analyzer.parse(w) if p.tag.number}
            return "plur" if s == {"plur"} else ("sing" if s == {"sing"} else "both")
        return x if (num(x) == "plur" and num(y) == "sing") else None
    if lang not in ("es", "fr"):
        return None
    x, y = _head_es_fr(wa, lang), _head_es_fr(wb, lang)
    if not x or not y or x.casefold() == y.casefold():
        return None
    xl, yl = x.casefold(), y.casefold()
    if not xl.endswith(_PLURAL_END) or yl.endswith(_PLURAL_END):
        return None
    # 要求同词（共享前缀），否则是换词不是变数
    k = min(len(xl), len(yl), 4)
    return x if xl[:k] == yl[:k] else None


def lex_norm(lang: str, analyzer):
    """给清单匹配用的词元化器。**只给俄语** —— 罗曼语这侧 simplemma 的单条输出
    不可采信（它抹平性、没上下文时给动词词元、还不幂等），清单匹配用字面实词更稳。
    """
    if lang != "ru" or analyzer is None:
        return None

    def f(w: str) -> set[str]:
        # 返回**全部**可能词元，不是 parse()[0].normal_form ——
        # 实测 `осадков` 给 `осадки`、`осадки` 给 `осадка`（另一个词），单值对不上。
        try:
            return {p.normal_form for p in analyzer.parse(w) if p.normal_form}
        except Exception:
            return set()

    return f


# ------------------------------------------------------------------ 各项检查
def check_dictform_consistency(rows: list[Row]) -> dict:
    """同一句中形式 -> 多个词典形。

    ⚠ **按「文件 + 切片」分组，不按切片单独分组**（2026-09-19 全量审计后改的）。
    交付路径上的 `unify_dict_forms` **有意只做同文件收口** —— 不同讲者、不同话题，
    没有理由把词典形强行统一（`extract.py` 里写着这条理由）。所以**跨文件**出现
    两个词典形不是缺陷，是设计。

    按切片单独分组会把 76 条设计内的差异算进「必须为 0」，于是这道门在**全量规模下
    永远不可达**（西语实测 82 条里 76 条跨文件），而一道常年红灯的门等于没有门 ——
    `fake_verification_patterns` 那条教训的反面：**门必须可达，否则没人看它。**

    ⚠ **分组键必须与 `extract.unify_dict_forms` 逐项相同**：`(文件, 中文术语, 外语切片)`。
    少了中文那一维，同形异义的条目会被算成分歧（`осадки` 既可以是「降水」也可以是
    「沉淀物」）；少了文件那一维，跨文件的差异会被算进来。**门与被门控的东西用不同的
    判据，是「门常亮」和「门骗人」两种病共同的根。** 实测：只按切片分组全量 82 处，
    加上文件维 6 处，再加上中文维 **0 处**。

    返回：`n` / `detail` = 同文件同中文下的分歧（这才是必须为 0 的，交付层也在这一步收口）；
    `n_cross` / `cross` = 其余分歧（只报不判，供人看译法是否该统一）。
    """
    strict = collections.defaultdict(set)
    loose = collections.defaultdict(set)
    for r in rows:
        if r.span and r.base:
            strict[(r.file, r.zh_term, r.span)].add(r.base)
            loose[(r.zh_term, r.span)].add(r.base)
    same = {k: sorted(v) for k, v in strict.items() if len(v) > 1}
    strict_keys = {k[1:] for k in same}          # (zh, span)
    cross = {k: sorted(v) for k, v in loose.items()
             if len(v) > 1 and k not in strict_keys}
    return {"n": len(same), "detail": same,
            "n_cross": len(cross), "cross": cross}


def _zh_compatible(row_zh: str, ref_zh: str) -> bool:
    """行的中文与清单条目的中文，是不是同一个概念。

    ⚠ **为什么必须有这一维**：`lookup_term` 只比**外语切片**、从不看中文 ——
    于是 `pueblos`（人民）会撞上清单里 `pueblos originarios`（原住民）的中心词，
    而 `pueblo`（人民）用单数**本来就是对的**（el pueblo = 人民）。
    同类的还有 `derechos`（权利）撞 `derechos humanos`（人权）、
    `órganos de derechos humanos`（人权机构，中心词该单数）撞 `derechos`。
    2026-09-19 全量审计：17 条 tier1 告警里多数是这一类假阳性，
    而**一条常年为假阳性的告警等于没有告警**。

    判据取**最宽**的一档（相等、或一方包含另一方）—— 只要不是明显不同概念就仍然报，
    宁可多留给人看。
    """
    a, b = (row_zh or "").strip(), (ref_zh or "").strip()
    if not a or not b:
        return True          # 缺一侧无法判，仍报
    return a == b or a in b or b in a


def _same_head(a: str, b: str) -> bool:
    """两个中心词是不是同一个词（大小写与首尾空白不敏感）。

    ⚠ 第三条判据用的：**掉复数的那个词，必须是清单条目自己的中心词**。
    清单说「`derechos humanos` 按 1. m. pl. 立目」；当
    `violaciones de los derechos humanos` 把中心词 `violaciones` 压成单数时，
    `derechos humanos` 在交付值里**原样保住了复数** —— 清单的主张已经被满足，
    这时报它才是错的（2026-09-19 全量实测：这一档 17 条里有 3 条是这样）。
    只有「条目的中心词自己掉了复数」才是这档要抓的东西。
    """
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def check_plural_dropped(rows: list[Row], lang: str, analyzer) -> dict:
    """中心词由复数变单数。**分两档**，见模块 docstring 第 3 条。"""
    tier1, tier2 = [], []
    nrm = lex_norm(lang, analyzer)
    for r in rows:
        h = head_number_dropped(r.span, r.base, lang, analyzer)
        if not h:
            continue
        # ⚠ 用**整条切片**查清单，不是中心词：`sources d'eau` 会撞上
        #   `sources de Baotu` 的中心词 `sources`，那条假阳性废掉了 tier1 的口径。
        e = lex_lookup_term(lang, r.span, nrm)
        item = {"span": r.span, "base": r.base, "head": h,
                "file": r.file, "sent_id": r.sent_id,
                "zh": r.zh_term, "fo_text": r.fo_text}
        if e and _zh_compatible(r.zh_term, e.zh) and _same_head(h, e.head):
            item["why"] = e.why
            item["zh_ref"] = e.zh
            tier1.append(item)
        else:
            if e:
                # 降档要说明原因，否则「清单命中却没报」看起来像漏报。
                item["zh_ref"] = e.zh
                if not _zh_compatible(r.zh_term, e.zh):
                    item["demoted"] = (f"清单中文是 {e.zh!r}，本行中文是 {r.zh_term!r}，"
                                       "不是同一个概念（lookup_term 只比外语切片）")
                else:
                    item["demoted"] = (f"掉复数的是中心词 {h!r}，而清单条目的中心词是 "
                                       f"{e.head!r} —— 条目自己的复数在交付值里保住了")
            tier2.append(item)
    return {"tier1": tier1, "tier2": tier2}


def check_complement(rows: list[Row], lang: str) -> dict:
    """补语的数被改（复用 extract.complement_num_changed；法语老师的口径）。"""
    out = []
    for r in rows:
        if not r.span or not r.base:
            continue
        cc = complement_num_changed(r.base, r.span, lang)
        if cc:
            out.append({"span": r.span, "base": r.base, "why": cc, "file": r.file})
    return {"n": len(out), "detail": out}


def check_zh_to_many(rows: list[Row]) -> dict:
    """同一中文术语 -> 多个外语词典形。混合三类，**只报不判**。"""
    m = collections.defaultdict(set)
    for r in rows:
        if r.zh_term and r.base:
            m[r.zh_term].add(r.base)
    bad = {k: sorted(v) for k, v in m.items() if len(v) > 1}
    return {"n": len(bad), "detail": bad}


def cross_language(by_lang: dict[str, list[Row]], analyzers: dict) -> dict:
    """同一中文术语在不同语种里外语侧的数不一致（数据库一致性轴）。"""
    def plural_like(r: Row, lang: str) -> bool | None:
        w = nom_words(r.base or r.span)
        if not w:
            return None
        if lang == "ru":
            a = analyzers.get("ru")
            if a is None:
                return None
            h = head_group(w, a)
            if not h or not a.word_is_known(h[-1]):
                return None
            s = {p.tag.number for p in a.parse(h[-1]) if p.tag.number}
            return True if s == {"plur"} else (False if s == {"sing"} else None)
        h = _head_es_fr(w, lang)
        return h.casefold().endswith(_PLURAL_END) if h else None

    seen = collections.defaultdict(dict)
    for lang, rows in by_lang.items():
        for r in rows:
            if not r.zh_term:
                continue
            p = plural_like(r, lang)
            if p is None:
                continue
            seen[r.zh_term].setdefault(lang, (p, r.base or r.span))
    bad = {}
    for zh, d in seen.items():
        if len(d) < 2:
            continue
        if len({v[0] for v in d.values()}) > 1:
            bad[zh] = {k: ("复数" if v[0] else "单数") + f" {v[1]!r}" for k, v in d.items()}
    return {"n": len(bad), "detail": bad}


# ------------------------------------------------------- 改动前后对照（多臂一致）
def _delivered_by_span(rows: list[Row]) -> dict[str, str]:
    """span -> 交付的词典形。同一 span 多条时取第一条（已断言过内部自洽为 0 分歧）。"""
    out: dict[str, str] = {}
    for r in rows:
        if r.span and r.base and r.span not in out:
            out[r.span] = r.base
    return out


def compare(base_rows: list[Row], arms: list[list[Row]], lang: str,
            analyzer) -> dict:
    """基线 vs 新版多臂。只认**全部新臂一致**的变化，其余记为抖动。"""
    b = _delivered_by_span(base_rows)
    maps = [_delivered_by_span(rs) for rs in arms]
    nrm = lex_norm(lang, analyzer)
    fixed, over, newly, jitter = [], [], [], []
    for span, old in b.items():
        vals = [m[span] for m in maps if span in m]
        if len(vals) < len(maps):
            continue                      # 新版没抽到这个 span，召回问题不在本表口径内
        if len(set(vals)) > 1:
            jitter.append({"span": span, "base": old, "new": sorted(set(vals))})
            continue
        new = vals[0]
        if new == old:
            continue
        old_dropped = head_number_dropped(span, old, lang, analyzer)
        new_dropped = head_number_dropped(span, new, lang, analyzer)
        item = {"span": span, "old": old, "new": new,
                "head": old_dropped or new_dropped or ""}
        if old_dropped and not new_dropped:
            # 基线压了中心词的数，新版没压 —— 命中清单的是「修好了」，否则要人看
            e = lex_lookup_term(lang, span, nrm)
            item["why"] = e.why if e else ""
            (fixed if e else over).append(item)
        elif new_dropped and not old_dropped:
            newly.append(item)
    return {"fixed": fixed, "over": over, "newly": newly, "jitter": jitter,
            "n_base": len(b), "n_arms": len(maps)}


def report_compare(lang: str, base: Path, arm_paths: list[Path], analyzer,
                   limit: int) -> int:
    base_rows, _n = load_rows(base)
    arms = [load_rows(p)[0] for p in arm_paths]
    c = compare(base_rows, arms, lang, analyzer)
    print("=" * 72)
    print(f"改动前后对照  语种 {lang}")
    print(f"  基线 {base.name}")
    for p in arm_paths:
        print(f"  新臂 {p.name}")
    print("=" * 72)
    print(f"口径：只认**{c['n_arms']} 个新臂给出相同值**的变化；不一致的记为抖动，不进结论。")
    print(f"      单条术语的值本身在噪声里，n=1 的逐条 diff 不能用来归因版本差异。")
    print()
    print(f"--- 修好了（基线把惯用复数压成单数，新版保留）：{len(c['fixed'])} 条 ---")
    for d in c["fixed"][:limit]:
        print(f"    ✓ {d['span']!r}  {d['old']!r} -> {d['new']!r}")
        if d.get("why"):
            print(f"        {d['why']}")
    print()
    print(f"--- 过度纠正（基线正确压成单数、新版却保留复数）：{len(c['over'])} 条 ---")
    print("    预注册门槛 <=3 条/语种。命中的要逐条看，不在清单里说明不是惯用复数。")
    for d in c["over"][:limit]:
        print(f"    ✗ {d['span']!r}  {d['old']!r} -> {d['new']!r}")
    print()
    print(f"--- 新出现的压单数（基线没动、新版压了）：{len(c['newly'])} 条 ---")
    for d in c["newly"][:limit]:
        print(f"    · {d['span']!r}  {d['old']!r} -> {d['new']!r}")
    print()
    print(f"--- 抖动（新臂之间就不一致，与版本无关）：{len(c['jitter'])} 条 ---")
    for d in c["jitter"][:limit]:
        print(f"    ~ {d['span']!r}  基线 {d['base']!r}  新臂 {d['new']}")
    print()
    print(f"基线可比 span {c['n_base']} 个")
    return len(c["over"])


# ---------------------------------------------------------------------- 报告
def report(lang: str, path: Path, rows: list[Row], notes: list[str],
           analyzer, limit: int) -> int:
    L = []
    a = L.append
    a("=" * 72)
    a(f"术语自检  语种 {lang}  来源 {path.name}")
    a("=" * 72)
    a(f"条数 {len(rows)}   其中已还原（词典形 ≠ 句中形式）"
      f"{sum(1 for r in rows if r.base and r.base != r.span)}")
    a("量的是 term_*_span（句中形式）与 term_*_base（词典形）两个显式列")
    for n in notes:
        a(n)
    a("")

    hard = 0
    c = check_dictform_consistency(rows)
    a(f"--- [机器判定] 同一文件内、同一中文术语、同一句中形式 -> 同一词典形："
      f"{c['n']} 处分歧（期望 0）---")
    for k, v in list(c["detail"].items())[:limit]:
        a(f"    {k!r} -> {v}")
    a(f"    分母：{len(rows)} 行。判据 = `(文件, 中文术语, 外语切片)`，")
    a("    与交付层 `unify_dict_forms` 的分组键**逐项相同** —— 门必须量交付层真收口的东西。")
    hard += c["n"]
    # 其余分歧（跨文件 / 同形异义 / 文件内未收口的残余）：只报不判。
    a("")
    a(f"--- [只报不判] 其余同一句中形式 -> 多词典形：{c['n_cross']} 组 ---")
    a("    跨文件不统一是**设计**（不同讲者不同话题）。看它是为了发现该统一的译法或上游错拼。")
    for k, v in list(c["cross"].items())[:limit]:
        a(f"    {k!r} -> {v}")

    if lang in ("es", "fr"):
        cp = check_complement(rows, lang)
        a("")
        # ⚠ 标签是 [只报不判]，不是 [机器判定] —— 它确实不计入退出码（见函数末尾），
        #   而且**不该**计入：法语唯一那条命中
        #   `aliments à base de farines` -> `aliment à base de farine`
        #   正是老师 ② 要的结果（表类别的补语规范成单数）。当闸门会把对的判成错的。
        a(f"--- [只报不判] 补语的数被改：{cp['n']} 条（法语老师的 ②③ 口径）---")
        a("    哪一类该单数（表类别）、哪一类该复数（表构成实体）是词汇知识，程序判不了。")
        for d in cp["detail"][:limit]:
            a(f"    {d['span']!r} -> {d['base']!r}  {d['why']}")

    pl = check_plural_dropped(rows, lang, analyzer)
    a("")
    a(f"--- [告警] 中心词由复数压成单数、且命中惯用复数清单：{len(pl['tier1'])} 条 ---")
    a("    这一档精确率高，**应当为 0**。命中即很可能是错的。")
    for d in pl["tier1"]:
        a(f"    ✗ {d['span']!r} -> {d['base']!r}   中心词 {d['head']!r}｜中文 {d['zh']!r}")
        a(f"        {d['why']}")
    a("")
    a(f"--- [抽样供扩清单，**不是错误列表**] 其余压成单数的：{len(pl['tier2'])} 条 ---")
    a("    绝大多数是对的（ISO 10241-1 §6.2.2 名词用单数）。列出来是为了让人发现")
    a("    清单里还缺哪些惯用复数词，发现了就加进 plural_lexicon.py。")
    for d in pl["tier2"][:limit]:
        a(f"    · {d['span']!r} -> {d['base']!r}")
    if len(pl["tier2"]) > limit:
        a(f"    …… 另有 {len(pl['tier2']) - limit} 条")

    z = check_zh_to_many(rows)
    a("")
    a(f"--- [只报不判] 同一中文术语 -> 多个外语词典形：{z['n']} 组 ---")
    a("    混合三类：合法变体（缩写/全称、不同音译）、上游错误、真错配。需人看。")
    for k, v in list(z["detail"].items())[:limit]:
        a(f"    {k!r} -> {v}")

    a("")
    # ⚠ tier1 必须连分母一起报。实测清单只有 32 条（es 11/fr 10/ru 11），
    #   交付里整条能命中清单的仅 22/1854 = 1.2% —— 「tier1 = 0」的真实含义是
    #   「被清单覆盖到的那几行里没有一条被压」，不是「压单数这件事没问题」。
    #   不带分母的 0 会被读成「干净」，那是 preregistered_criteria_denominator 的教训。
    _cov = sum(1 for r in rows if lex_lookup_term(lang, r.span, lex_norm(lang, analyzer)))
    _n_pl = len(pl["tier1"]) + len(pl["tier2"])
    _pct = f"{_cov / len(rows):.1%}" if rows else "n/a"
    a("")
    a(f"结构性问题（必须为 0）：同一句中形式多词典形 {c['n']}")
    a(f"高精确告警（应为 0）：惯用复数被压单数 {len(pl['tier1'])}"
      f"   ← 分母：清单 {len(_HABITUAL.get(lang, ()))} 条覆盖 {_cov}/{len(rows)} 行"
      f"（{_pct}），压单数共 {_n_pl} 条")
    if not _cov:
        a("    ⚠ 覆盖 0 行 —— 这一档此刻**不携带任何信息**，别把 0 读成「干净」。")
    print("\n".join(L))
    return hard + len(pl["tier1"])


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="getterms.selfcheck",
                                 description="三语术语质量自检（不调 API）")
    ap.add_argument("--lang", choices=["es", "fr", "ru"], default=None)
    ap.add_argument("--source", default=None, help="dump 路径；不给则用该语种定稿 dump")
    ap.add_argument("--all", action="store_true", help="三语都跑，并做跨语种一致性")
    ap.add_argument("--limit", type=int, default=15, help="每类明细最多打几条")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="形态分析器缺失时仍然跑（默认拦住：降级下依赖词元的判据"
                         "静默失效，「高精确告警 0」是空真的）。")
    ap.add_argument("--compare", default=None,
                    help="基线 dump；与 --arms 一起用，做改动前后的「数」对照")
    ap.add_argument("--arms", nargs="*", default=None,
                    help="新版 dump（同一提示词的多次采样）。只认全部臂一致的变化")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    langs = ["es", "fr", "ru"] if a.all or not a.lang else [a.lang]
    analyzers = {"ru": load_morph(), "es": load_lemmatizer(), "fr": load_lemmatizer()}

    # ⚠ 形态分析器的状态必须摆在最前面，缺库要拦 —— 与 run.py 的硬闸门同口径。
    #   2026-09-19 实测：本机 PowerShell 默认的 python 没装 simplemma/pymorphy3，
    #   而 selfcheck 在两个环境里输出**完全一样**（都是「高精确告警 0」）。
    #   依赖词元的判据（共享词元、不得复数化、tier1 的清单词元匹配）在降级下静默失效，
    #   于是「selfcheck 干净」这个全量前置条件可能是**空真**。
    for lg in langs:
        print(f"[{lg}] 形态分析器 {morph_status(lg)}")
    _degraded = [lg for lg in langs
                 if lg in DICT_FORM_LANGS and analyzers.get(lg) is None]
    if _degraded and not a.allow_degraded:
        print(f"\n[停止] {'/'.join(_degraded)} 的形态分析器没启用，"
              "此时依赖词元的判据会静默失效，\n"
              "        而报告照印「高精确告警 0」—— 这个 0 是空真的。\n"
              "        先装依赖：pip install simplemma pymorphy3 pymorphy3-dicts-ru\n"
              "        ⚠ 先确认解释器：本机 PowerShell 默认的 python 与 Bash 的不是"
              "同一个，只有 miniconda3 那个装了。\n"
              "        确认要看降级下的结果，再加 --allow-degraded。")
        return 2

    if a.compare:
        if not a.lang:
            print("--compare 要配 --lang")
            return 2
        if not a.arms:
            print("--compare 要配 --arms 新版 dump（建议 >=2 臂，见 report_compare 的口径）")
            return 2
        return 0 if report_compare(
            a.lang, Path(a.compare), [Path(x) for x in a.arms],
            analyzers.get(a.lang), a.limit) <= 3 else 1
    by_lang: dict[str, list[Row]] = {}
    bad_total = 0
    for lang in langs:
        p = Path(a.source) if (a.source and len(langs) == 1) else DEFAULT_SOURCE[lang]
        if not p.exists():
            print(f"[跳过] {lang}: 找不到 {p}")
            continue
        rows, notes = load_rows(p)
        by_lang[lang] = rows
        bad_total += report(lang, p, rows, notes, analyzers.get(lang), a.limit)
        print()

    if len(by_lang) > 1:
        x = cross_language(by_lang, analyzers)
        print("=" * 72)
        print(f"--- [只报不判] 跨语种同一中文术语的数不一致：{x['n']} 组 ---")
        print("    数据库一致性轴。已知实证：「资本市场」es/fr 复数、ru 单数。")
        for k, v in list(x["detail"].items())[:a.limit]:
            print(f"    {k!r} -> {v}")

    print()
    print(f"=== 需要处理的（结构问题 + 高精确告警）：{bad_total} ===")
    return 0 if bad_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
