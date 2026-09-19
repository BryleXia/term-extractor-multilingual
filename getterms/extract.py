"""抽取核心：分批 -> 调用 -> 容错解析 JSON -> 逐字锚定校验 -> 纠正重试。

抗幻觉的两道闸：
  1. 提示词里的硬约束（term 必须是原文连续子串）；
  2. 本模块的程序化校验 —— 校验不过就纠正重试一次，仍不过则丢弃并记录。
xlsx 里装术语的有两列：**第 4/5 列**（`term_src`/`term_tgt`）一律**取自原文的真实
切片**，不是模型回抄的字符串，所以下游拿到的每个术语都保证能在原句里定位
（转 `final.json` 的内联标注 `[...]{类型}` 就靠它）；**第 8/9 列**
（`term_*_dict`）是词条形式，见 `DICT_FORM_LANGS` 与 `out_src`。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

from .config import ALLOWED_TYPES, PROMPT_DIR
from .corpus import CorpusFile, Sentence, cjk_ratio
from .llm import CallResult, LLMClient, prompt_hash

USER_TEMPLATE_DEFAULT = (
    "\nThe following is a list of sentence pairs.\n"
    "For EACH sentence, extract candidate terms.\n\nInput:\n{pairs}\n"
)
USER_DELIM = "===USER==="


# --------------------------------------------------------------- 提示词加载

def load_prompt(name: str) -> tuple[str, str, str]:
    """读提示词文件，返回 (system, user_template, prompt_hash)。

    文件默认整体是 system 提示词；如含 '===USER===' 分隔行，
    其后的部分作为 user 模板（必须含 {pairs} 占位符）。
    语言学者可以直接改这个文件，不用碰代码。
    """
    path = PROMPT_DIR / (name if name.endswith(".md") else f"{name}.md")
    if not path.exists():
        avail = sorted(p.stem for p in PROMPT_DIR.glob("*.md"))
        raise FileNotFoundError(f"找不到提示词 {path}；已有: {avail}")
    text = path.read_text(encoding="utf-8")
    if USER_DELIM in text:
        system, user = text.split(USER_DELIM, 1)
        system, user = system.strip(), user.strip()
        if "{pairs}" not in user:
            raise ValueError(f"{path} 的 user 段缺少 {{pairs}} 占位符")
    else:
        system, user = text.strip(), USER_TEMPLATE_DEFAULT
    return system, user, prompt_hash(system, user)


def build_messages(system: str, user_template: str,
                   batch: list[Sentence]) -> list[dict]:
    """照 HTML 的形状构造消息：pairs 是 [{sent_id, src, tgt}] 的两空格缩进 JSON。

    注意 sent_id 送的是**内部位置索引**（1..N），不是原文里可能重复/为 0 的 sent_id。
    """
    pairs = [{"sent_id": s.pos, "src": s.src, "tgt": s.tgt} for s in batch]
    user = user_template.replace(
        "{pairs}", json.dumps(pairs, ensure_ascii=False, indent=2)
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def batch_key(batch: list[Sentence]) -> str:
    """批内容的短哈希，进缓存文件名，保证换批大小不会命中错误的旧响应。

    字段之间必须有分隔符，否则 (pos=1, src="ab") 与 (pos=1, src="a", tgt="b…")
    会拼成同一串而碰撞。
    """
    h = hashlib.sha256()
    sep = bytes([31])          # 单元分隔符
    rec = bytes([30])          # 记录分隔符
    for s in batch:
        h.update(str(s.pos).encode("utf-8"))
        h.update(sep)
        h.update(s.src.encode("utf-8"))
        h.update(sep)
        h.update(s.tgt.encode("utf-8"))
        h.update(rec)
    return h.hexdigest()[:8]


# ⚠ 注意：`run.py:187` 自己内联重写了一遍同样的分批逻辑，没有调用本函数。
#   两份逻辑并存 —— 改这里**不会**影响交付主路径。临近全量不做重构，
#   但谁改分批规则必须同时改 run.py 那一处，否则缓存键与批内容会分叉。
def make_batches(f: CorpusFile, batch_size: int) -> list[list[Sentence]]:
    """只把两侧都有实义内容的句子送模型（§2 行 10/11）。"""
    us = f.usable_sentences
    return [us[i:i + batch_size] for i in range(0, len(us), batch_size)]


# --------------------------------------------------------------- 容错 JSON

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_FULLWIDTH = {
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "＂": '"', "＇": "'", "，": ",", "：": ":",
    "［": "[", "］": "]", "｛": "{", "｝": "}",
}


def parse_json_array(text: str) -> list:
    """尽最大努力从模型输出里取出 JSON 数组（只要结果，不问成败）。"""
    return parse_json_array_ex(text)[0]


def parse_json_array_ex(text: str) -> tuple[list, bool]:
    """同上，但**第二个返回值说明「到底解析成功了没有」**。

    为什么必须分开（2026-09-19 夜）：本函数对下面两种输入都返回 `[]`，
    而 `validate_batch:895` 看到空的 items 就置 `json_failed = True`：

    - **(a) 解析失败** —— 模型给的是散文 / 半截 JSON，我们**丢失了**这批的答案；
    - **(b) 合法空数组 `[]`** —— 模型**明确说**这批没术语，这是**最终答案**。

    两者在交付物里的表现一样（那几句没有术语），所以进闸门同权是对的；
    但**该不该撤缓存**上两者相反：(a) 不撤就永远补不上，(b) 撤了就每次重跑
    都为「确实没术语」重新付费。tour/serv 层有术语句仅 51%，一批 10 句全无
    术语的概率约 0.49^10，全量 18,605 批里约十几批 —— 不多，但没理由白付。

    依次尝试：直接解析 -> 剥围栏 -> json_object 包装（{"result":[...]}）->
    截取最外层 [] -> 全角标点转半角 -> 去尾逗号。

    依次尝试：直接解析 -> 剥围栏 -> json_object 包装（{"result":[...]}）->
    截取最外层 [] -> 全角标点转半角 -> 去尾逗号。
    """
    if not text or not text.strip():
        return [], False
    cands: list[str] = []
    raw = text.strip()
    cands.append(raw)
    stripped = _FENCE.sub("", raw).strip()
    if stripped != raw:
        cands.append(stripped)
    for base in list(cands):
        m = re.search(r"\[[\s\S]*\]", base)
        if m:
            cands.append(m.group(0))
    extra = []
    for c in cands:
        t = c
        for k, v in _FULLWIDTH.items():
            t = t.replace(k, v)
        t = _TRAILING_COMMA.sub(r"\1", t)
        if t != c:
            extra.append(t)
    cands.extend(extra)

    for c in cands:
        try:
            data = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            return data, True
        if isinstance(data, dict):
            # response_format=json_object 时模型常包一层
            for k in ("result", "results", "data", "items", "sentences", "output"):
                v = data.get(k)
                if isinstance(v, list):
                    return v, True
            # 单个句子对象
            if "sent_id" in data:
                return [data], True
    return [], False


# --------------------------------------------------------------- 逐字锚定

def _fold_yo(s: str) -> str:
    """把 ё/Ё 折成 е/Е，**逐字符一对一，长度不变**，所以下标可安全回切。

    为什么需要（俄语测试语料实测）：语料自己就 ё/е 混用 —— 8 个词同时存在两种拼法
    （`всё`(3) 与 `все`(43)、`ещё`(2) 与 `еще`(21)、`ведёт` 与 `ведет` …）。
    俄语书面习惯常省掉两点，模型两种都可能输出，不折叠就会锚定失败、术语被白丢。

    ⚠ 这是**正字法变体**，与屈折（词尾变化）是两回事，折叠它不改变词义、没有语义风险。
    屈折要不要宽容是另一个问题，判据见 计划_俄语.md §1。
    """
    return s.replace("ё", "е").replace("Ё", "Е")


def _collapse_ws(s: str) -> tuple[str, list[int]]:
    """折叠连续空白，同时保留每个输出字符对应的原始下标。"""
    out: list[str] = []
    idx: list[int] = []
    prev_space = False
    for i, ch in enumerate(s):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def find_verbatim(term: str, text: str) -> str | None:
    """在 text 里找 term，返回**取自 text 的真实切片**；找不到返回 None。

    容忍度依次放宽：精确 -> 大小写不敏感 -> **ё/е 折叠（俄语）** -> 空白折叠。
    始终返回原文切片，绝不返回模型写的字符串。

    ⚠ 刻意**不做**形态学（屈折）宽容：那会放松反幻觉主闸门。俄语屈折的处理方式
    见 计划_俄语.md §1 —— 先用提示词要求模型照抄屈折形式并实测失败率，达不到判据才动这里。
    """
    if not term or not text:
        return None
    t = unicodedata.normalize("NFC", term).strip()
    txt = unicodedata.normalize("NFC", text)
    if not t:
        return None

    i = txt.find(t)
    if i >= 0:
        return txt[i:i + len(t)]

    # 大小写不敏感（casefold 可能改变长度，等长才安全回切）
    lt, ltxt = t.casefold(), txt.casefold()
    if len(lt) == len(t) and len(ltxt) == len(txt):
        i = ltxt.find(lt)
        if i >= 0:
            return txt[i:i + len(t)]

    # ё/е 折叠（俄语正字法变体）。等长映射，回切安全；仍然返回原文切片。
    yt, ytxt = _fold_yo(lt), _fold_yo(ltxt)
    if len(yt) == len(t) and len(ytxt) == len(txt):
        i = ytxt.find(yt)
        if i >= 0:
            return txt[i:i + len(t)]

    # 空白折叠后再找，用下标映射回原文
    ct, _ = _collapse_ws(t)
    ctxt, cmap = _collapse_ws(txt)
    lct, lctxt = _fold_yo(ct.casefold()), _fold_yo(ctxt.casefold())
    if len(lct) == len(ct) and len(lctxt) == len(ctxt):
        i = lctxt.find(lct)
        if i >= 0 and ct:
            start = cmap[i]
            end = cmap[i + len(ct) - 1] + 1
            return txt[start:end]
    return None


# -------------------------------------------------- 词典形（一格）还原与校验
#
# 为什么会有这一层：俄语老师 2026-09-17 定的口径 —— 术语库要收术语的**原型**，
# 不是句中的二三四五六格。但逐字锚定是全项目的反幻觉主闸门，不能拆。
# 折中：锚定照旧跑，产物降级为「内部校验值 + 溯源值」；词典形走独立字段，
# 并用下面这套判据验证它**确实是那个词的还原**，而不是模型另写的一个词。

_MORPH: dict = {}


def load_morph():
    """懒加载 pymorphy3。缺库/缺词典/词典不兼容一律返回 None，全流程降级不报错。

    为什么需要形态分析器：老师要的是「真的还原到一格」，而纯字符串规则只能验
    「像是同一个词的变体」，验不了「是不是主格」。pymorphy3 是纯 Python +
    独立词典包（pymorphy3-dicts-ru），无 GPU、无重依赖，装不上也只是降级。
    """
    if "m" not in _MORPH:
        try:
            import pymorphy3
            _MORPH["m"] = pymorphy3.MorphAnalyzer()
            _MORPH["why"] = ""
        except Exception as e:          # 缺库、缺词典、版本不兼容都走这里
            _MORPH["m"] = None
            _MORPH["why"] = f"{type(e).__name__}: {e}"
    return _MORPH["m"]


_LEMMA: dict = {}


def load_lemmatizer():
    """懒加载 simplemma（西语/法语的词形还原器）。缺库返回 None，降级不报错。

    角色与俄语的 pymorphy3 对等：验「模型给的词典形与句中形式是不是同一个词的
    两种形态」。simplemma 是纯 Python + 内置词典（19MB wheel），无重依赖。
    """
    if "m" not in _LEMMA:
        try:
            import simplemma
            simplemma.lemmatize("casas", lang="es")      # 触发词典加载，早失败
            _LEMMA["m"] = simplemma
            _LEMMA["why"] = ""
        except Exception as e:          # noqa: BLE001
            _LEMMA["m"] = None
            _LEMMA["why"] = f"{type(e).__name__}: {e}"
    return _LEMMA["m"]


def morph_status(lang: str = "ru") -> str:
    """给报告用的一行状态。按语种说清用的是哪个分析器、有没有启用。"""
    if lang == "ru":
        if load_morph() is not None:
            return "pymorphy3 已启用（中心组主格 + 词元共享 + 不得复数化校验生效）"
        return ("pymorphy3 未启用，降级为关系校验（词数/词干/字符集）："
                + (_MORPH.get("why") or "未安装"))
    if load_lemmatizer() is not None:
        return "simplemma 已启用（词元共享 + 不得复数化校验生效）"
    return ("simplemma 未启用，降级为关系校验（词数/功能词/专名/字符集）："
            + (_LEMMA.get("why") or "未安装"))


# 词边缘要剥掉的标点。不含空白 —— split() 已经把空白处理掉了。
_NOM_EDGE = ".,;:!?()[]{}«»—–…·" + '"' + "'" + "“”‘’"


def nom_words(s: str) -> list[str]:
    """按空白切词并剥掉边缘标点。连字符留在词内（`военно-политические` 是一个词）。"""
    return [w for w in (x.strip(_NOM_EDGE) for x in s.split()) if w]


def _has_cyr(s: str) -> bool:
    return any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in s)


def _latin(s: str) -> set:
    return {c.lower() for c in s if ("a" <= c <= "z") or ("A" <= c <= "Z")}


def _cjk_chars(s: str) -> set:
    return {c for c in s if "一" <= c <= "鿿"}


def _stem_prefix_ok(a: str, b: str) -> bool:
    """两个词是否共享足够长的词干。俄语屈折改词尾，共享词干是正确的不变量。

    阈值 max(3, ceil(0.6 * 较短者长度))，先做 ё→е 折叠。挡的是「模型换了个词」，
    不是挡屈折。已知边界：词干本身发生变化的还原（`дня` -> `день`）会被判失败，
    这类会走回落（保留原句切片），不会丢术语。
    """
    a, b = _fold_yo(a.casefold()), _fold_yo(b.casefold())
    n = min(len(a), len(b))
    if n == 0:
        return False
    need = min(n, max(3, -(-6 * n // 10)))
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= need


def head_group(words: list[str], morph) -> list[str]:
    """短语的中心组：开头连续的修饰语 + 第一个能解析成名词的词。

    为什么只查这一组（2026-09-17 pymorphy3 实测得出，不是拍脑袋）：
    从属成分**必须**保留自己的格 —— `империя лжи` 的 `лжи` 是属格、
    `война с нацизмом` 的 `нацизмом` 是工具格，都是词典惯例。要求全词主格
    会把正确答案判成错。
    反过来，只要中心组全是主格，「压根没还原」就跑不掉：
      * `искусственного интеллекта` 两个词都没有主格解
      * `холодной войны` 的 `холодной` 没有主格解（尽管 `войны` 有主格复数同形）
      * `Украины` 没有主格解（`Украина` 是 Sgtm 单数专有，不存在主格复数）
    """
    out: list[str] = []
    for w in words:
        out.append(w)
        if not _has_cyr(w):
            continue
        if any("NOUN" in str(p.tag) for p in morph.parse(w)):
            break
    return out


# 名词性词类：只有这些词才谈得上「格」。副词/语气词/连词/介词没有格。
_NOMINAL_POS = {"NOUN", "ADJF", "ADJS", "PRTF", "PRTS", "NPRO", "NUMR", "ADVB_NO"}


def _same_lexeme_ru(x: str, y: str, m) -> bool:
    """pymorphy3 认为这两个俄语词形属于同一个词吗（共享词元）。

    给判据 2 做覆盖用：词干前缀分岔但形态层确认同词 -> 放行。
    ⚠ 不要求 `word_is_known` —— 连字符复合词（`яму-гребницу`）两侧都是词典外的，
      而 pymorphy3 的预测器照样给出一致的 `normal_form`，那正是我们要的证据。
      为防预测器乱认，再要求共享**首字母**。
      ⚠ 起初写的是「共享前 2 个字符」，被 `дня` -> `день` 推翻：俄语的逃逸元音
        （беглая гласная）就发生在第 2 个字符上（`де`/`дн`），而这恰恰是本次要修的
        那一类。共享词元本身已经是强证据 —— 真·换词（`собаку` vs `кошка`）的
        normal_form 集合根本不相交，首字母只是挡跨字母表的兜底。
    """
    if not (_has_cyr(x) and _has_cyr(y)):
        return False
    a, b = _fold_yo(x.casefold()), _fold_yo(y.casefold())
    if a[:1] != b[:1]:
        return False
    try:
        nx = {_fold_yo(p.normal_form) for p in m.parse(x)}
        ny = {_fold_yo(p.normal_form) for p in m.parse(y)}
    except Exception:       # noqa: BLE001 —— 分析器出错就当没有证据
        return False
    return bool(nx and ny and (nx & ny))


def _is_nominal_ru(w: str, m) -> bool:
    """这个词有没有名词性解析。全是副词/语气词/连词/介词的，谈不上「格」。"""
    try:
        ps = m.parse(w)
    except Exception:       # noqa: BLE001
        return True         # 拿不到解析就按老口径继续查，不放松
    if not ps:
        return True
    return any(str(p.tag.POS or "") in ("NOUN", "ADJF", "ADJS", "PRTF", "PRTS",
                                        "NPRO", "NUMR") for p in ps)


def check_nominative(base: str, anchored: str, morph=None,
                     use_morph: bool = True) -> str | None:
    """校验词典形 `base` 与原句切片 `anchored` 的关系。返回错误说明，None = 通过。

    前三项不依赖任何库；后两项需要 pymorphy3，缺库自动跳过（降级）。
    传 `use_morph=False` 可强制走降级路径（断言里要覆盖这条路径）。
    """
    b = unicodedata.normalize("NFC", (base or "").strip())
    a = unicodedata.normalize("NFC", (anchored or "").strip())
    if not b:
        return "dict_form 为空"
    if not a:
        return "原句切片为空"

    wb, wa = nom_words(b), nom_words(a)
    if not wb or not wa:
        return "切不出词"

    # 1) 词数一致 —— 还原格不会增删词，词数变了说明模型在改写术语
    if len(wb) != len(wa):
        return (f"词数不一致：dict_form {len(wb)} 词 / 原句切片 {len(wa)} 词"
                "（还原格不会增删词）")

    # 2) 逐词词干一致。**先只记下来**，判定推迟到拿到形态分析器之后 ——
    #    词干前缀是「库不可用时」的兜底启发式，形态层能证明是同一个词时不该由它说了算。
    #    2026-09-18 实测被它误拒的正确还原：`яму`->`яма`、`угля`->`уголь`（元音交替）、
    #    `яму-гребницу`->`яма-гребница`、`лесов-пагод`->`лес-пагода`（连字符复合词）。
    stem_bad = None
    for i, (x, y) in enumerate(zip(wb, wa), 1):
        if not _stem_prefix_ok(x, y):
            stem_bad = (i, x, y)
            break

    # 3) 不得引入原文没有的字符集（挡「顺手翻译成中文/拉丁转写」）
    new_lat = _latin(b) - _latin(a)
    if new_lat:
        return f"引入了原文没有的拉丁字母 {sorted(new_lat)}"
    new_cjk = _cjk_chars(b) - _cjk_chars(a)
    if new_cjk:
        return f"引入了原文没有的汉字 {sorted(new_cjk)}"

    def _stem_verdict() -> str | None:
        if stem_bad is None:
            return None
        i, x, y = stem_bad
        return f"第 {i} 个词 {x!r} 与原文 {y!r} 词干不符（像是换了词，不是还原）"

    if not use_morph:
        return _stem_verdict()
    m = morph if morph is not None else load_morph()
    if m is None:
        return _stem_verdict()          # 降级：只跑前三项，词干前缀说了算

    # 判据 2 的最终判定：形态层证明是同一个词就放行（罗曼语那侧一直是这个并集口径）
    if stem_bad is not None and not _same_lexeme_ru(stem_bad[1], stem_bad[2], m):
        return _stem_verdict()

    # 4) 中心组必须全是主格 —— 这才是真正验「是不是一格」
    #
    #    ⚠ 只查**词典里认识**的词（`word_is_known`）。2026-09-17 实测：280 句样本
    #    的 7 条回落全是假阳性，且全是词典外的词 —— 音译人名 `Пэн`/`Лю`/`Даг`、
    #    中文地名 `Бифэнься`、缩写 `МФК`。对这些词 pymorphy3 只能靠预测器猜，
    #    猜不出主格并不构成「没还原」的证据。
    #    而真正该抓的三个反例全部 known=True，收紧后照旧被抓：
    #      Украины / холодной / искусственного。
    #    已知残余：`Шан`（`Пэн Шан Мин` 的中间字）恰好是词典里一个只有斜格的名字，
    #    仍会判失败 —— 但这类 base 与切片本来相同，回落交付的是同一个字符串，
    #    交付值无损失，只是报告里多一条噪声。
    for w in head_group(wb, m):
        if not _has_cyr(w) or not m.word_is_known(w):
            continue
        # ⚠ 2026-09-18 新增：副词/语气词/连词/介词**没有格**，要求它们有主格解
        #   是范畴错误。实测误拒 `полностью сбалансированный вертикальный
        #   судоподъёмник`（副词 `полностью`）与 `Не Жу Чжэнь`（`Не` 被解析成语气词）。
        #   只跳过「全部解析都不是名词性」的词 —— 有一个名词解就照旧检查，
        #   真正的漏还原（Украины / холодной / искусственного）照旧被抓。
        if not _is_nominal_ru(w, m):
            continue
        ps = m.parse(w)
        if ps and not any("nomn" in str(p.tag) for p in ps):
            return f"{w!r} 没有主格解，中心词组没有还原到一格"

    # 5) 逐词共享词元 —— 形态上必须是同一个词。同样只在两侧都认识时才判：
    #    `изюбрий`（属格复数，词典外）与正确还原 `изюбри` 的词元集不相交，
    #    那是词典缺口，不是模型换了词。
    for i, (x, y) in enumerate(zip(wb, wa), 1):
        if not (_has_cyr(x) and _has_cyr(y)):
            continue
        if not (m.word_is_known(x) and m.word_is_known(y)):
            continue
        nx = {_fold_yo(p.normal_form) for p in m.parse(x)}
        ny = {_fold_yo(p.normal_form) for p in m.parse(y)}
        if nx and ny and not (nx & ny):
            return f"第 {i} 个词 {x!r} 与原文 {y!r} 不共享词元（形态上不是同一个词）"

    # 6) 不得复数化 —— 原文是单数，词典形反而变复数，方向搞反了
    #
    #    2026-09-18 新增。ISO 10241-1 §6.2.2 与 IATE 手册都要求名词条目默认单数，
    #    所以 ru_v5 起「数」也要还原（v4 是「数不动」）。这条只挡**反方向**：
    #    单数 -> 复数。正方向（复数 -> 单数）本来就是本轮要的，
    #    而「习惯复数保留」（`совместные учения`）程序判不了，靠提示词 + 老师抽检。
    #    同形歧义放过：`экосистемы` 有主格复数解，不会被误判。
    for i, (x, y) in enumerate(zip(wb, wa), 1):
        if not (_has_cyr(x) and _has_cyr(y)):
            continue
        if not (m.word_is_known(x) and m.word_is_known(y)):
            continue
        px, py = m.parse(x), m.parse(y)
        if not px or not py:
            continue
        x_plur_only = all(p.tag.number == "plur" for p in px)
        y_sing_only = all(p.tag.number == "sing" for p in py)
        if x_plur_only and y_sing_only:
            return (f"第 {i} 个词 {x!r} 是复数，原文 {y!r} 是单数"
                    "（词典形不该把单数改成复数）")
    return None


# 冠词/介词/连词：在术语跨度里出现时**不参与还原**，必须逐字相同。
# 西语的冠词本来就不入 span（es_v3 没有这条规则，法语 fr_v4 有），
# 但短语内部的 `de la` / `del` 一定会出现（`luces de neón`、`jarrones de la guardia`）。
_FUNCTION_WORDS = {
    "es": {"de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
           "y", "e", "o", "u", "en", "a", "al", "con", "por", "para", "su", "sus"},
    "fr": {"de", "du", "des", "d'", "la", "le", "les", "l'", "un", "une",
           "et", "ou", "en", "à", "au", "aux", "son", "sa", "ses", "ce", "cette"},
}


def _lemma_of(lem, w: str, lang: str) -> str:
    """单次词形还原。⚠ **绝不二次还原** —— 实测 lemmatize 不幂等：
    `Estados` -> `estado` -> `estar`，第二次会跑到动词上去。
    """
    try:
        return lem.lemmatize(w, lang=lang)
    except Exception:       # noqa: BLE001 —— 词典缺失/编码异常一律当「不认识」
        return w


def _is_known(lem, w: str, lang: str) -> bool:
    try:
        return bool(lem.is_known(w, lang=lang))
    except Exception:       # noqa: BLE001
        return False


def _same_word(lem, base_w: str, span_w: str, lang: str) -> bool:
    """两个词是不是同一个词的两种形态。四条证据取「或」，任一成立即通过。

    ⚠ 为什么是「共享词元」而不是「等于词元」（2026-09-18 实测定的，不是推理）：
      * simplemma 会把**性抹平**：`privadas` 与 `privada` 的词元都是 `privado`，
        若要求 base 等于词元，正确还原 `empresa privada` 会被判失败。
      * simplemma 在没有词性上下文时会给**动词**词元：`flores` -> `florar`、
        `Unidos` -> `unir`、`desplazados` -> `desplazar`、`estado` -> `estar`。
        单靠词元比对会误杀 `flor y pájaro`、`desplazado` 这些正确还原，
        所以并联一条词干前缀支。
    实测 55 个用例（含 3 个换词幻觉）：误杀 0、漏放 0。
    """
    if base_w.casefold() == span_w.casefold():
        return True
    if lem is not None:
        lb = _lemma_of(lem, base_w, lang).casefold()
        ls = _lemma_of(lem, span_w, lang).casefold()
        if lb == ls or ls == base_w.casefold() or lb == span_w.casefold():
            return True
    return _stem_prefix_ok(base_w, span_w)


def _gender_flipped(base_w: str, span_w: str, lang: str) -> bool:
    """词尾的性别标记有没有被翻转。纯后缀规则，不依赖任何库。

    补的是 simplemma 的一个硬缺口：它**把性抹平**（`privadas`、`privada` 的词元
    都是 `privado`），所以判据 5 放得过 `empresa privado` 这种把阴性形容词
    错还原成阳性的答案。而 ISO 10241-1 §6.2.9.3.1 是「标注」名词的性，
    不是把性规范掉；`la política`（政策）与 `el político`（政客）是两个词。

    实测（2026-09-18，`_probe_simplemma3.py`）：7 个真实性别错误全部命中；
    83 条人工写下的正确还原**零误杀**。功能词由判据 2 先拦下，走不到这里。
    """
    b, a = base_w.casefold(), span_w.casefold()
    if b == a:
        return False
    if lang == "es":
        # 西语的性别标记就是词尾 -o/-os（阳）与 -a/-as（阴），两者不会跨着变数
        b_f, b_m = b.endswith(("a", "as")), b.endswith(("o", "os"))
        a_f, a_m = a.endswith(("a", "as")), a.endswith(("o", "os"))
        return (a_f and b_m) or (a_m and b_f)
    if lang == "fr":
        # 法语阴性标记是词尾 -e；复数只是加 -s，所以 -es 蕴含单数 -e。
        # -x/-s/-z 结尾的不规则复数（travaux、généraux）不适用，排除掉。
        a_f, b_f = a.endswith(("e", "es")), b.endswith("e")
        if a_f and not b_f:
            return True
        return (not a_f) and b_f and not a.endswith(("x", "s", "z"))
    return False


def check_lemma_romance(base: str, anchored: str, lang: str,
                        lemmatizer=None, use_lemmatizer: bool = True) -> str | None:
    """校验西语/法语的词典形 `base` 与原句切片 `anchored` 的关系。

    返回错误说明，None = 通过。前四项不依赖任何库；后两项需要 simplemma，
    缺库自动跳过（降级）。传 `use_lemmatizer=False` 可强制走降级路径。

    与俄语 `check_nominative` 的分工：俄语要还原格（6 格 -> 主格）+ 数；
    西/法没有格，只还原数，并且**名词的性绝不改**（`la política` 政策 与
    `el político` 政客 是两个词 —— ISO 10241-1 §6.2.9.3.1 是「标注」性，不是规范掉）。
    """
    b = unicodedata.normalize("NFC", (base or "").strip())
    a = unicodedata.normalize("NFC", (anchored or "").strip())
    if not b:
        return "dict_form 为空"
    if not a:
        return "原句切片为空"

    wb, wa = nom_words(b), nom_words(a)
    if not wb or not wa:
        return "切不出词"

    # 1) 词数一致 —— 还原数不会增删词
    if len(wb) != len(wa):
        return (f"词数不一致：dict_form {len(wb)} 词 / 原句切片 {len(wa)} 词"
                "（还原不会增删词）")

    fw = _FUNCTION_WORDS.get(lang, set())
    for i, (x, y) in enumerate(zip(wb, wa), 1):
        # 2) 功能词（冠词/介词/连词）不参与还原，必须逐字相同
        if y.casefold() in fw or x.casefold() in fw:
            if x.casefold() != y.casefold():
                return (f"第 {i} 个词：功能词 {y!r} 被改成 {x!r}"
                        "（冠词/介词/连词不参与还原）")
            continue
        # 4) 专名不动：原文里首字母大写的词，词典形必须原样保留
        #    ⚠ 已知边界：句首被大写的普通名词（`Impuestos …`）会因此不被还原 ->
        #    回落成句中形式，交付值无损，只是少还原一条。宁可少还原不要错还原。
        if y[:1].isupper() and x != y:
            return (f"第 {i} 个词：专名 {y!r} 被改成 {x!r}"
                    "（原文里首字母大写的词不还原）")
        # 7) 性别标记不许翻转（纯后缀，不依赖库）
        if _gender_flipped(x, y, lang):
            return (f"第 {i} 个词：{y!r} 被改成 {x!r}，词尾性别标记翻转了"
                    "（名词的性是固有属性，不参与还原；形容词要与中心名词一致）")

    # 3) 不得引入原文没有的字符集
    #    ⚠ 刻意**不做**拉丁字母集合检查 —— `resoluciones` -> `resolución`
    #    会多出带重音的 `ó`，那是正确还原。只挡跨文字系统的窜改。
    new_cjk = _cjk_chars(b) - _cjk_chars(a)
    if new_cjk:
        return f"引入了原文没有的汉字 {sorted(new_cjk)}"
    cyr_b = {c for c in b if _has_cyr(c)}
    cyr_a = {c for c in a if _has_cyr(c)}
    if cyr_b - cyr_a:
        return f"引入了原文没有的西里尔字母 {sorted(cyr_b - cyr_a)}"

    if not use_lemmatizer:
        return None
    lem = lemmatizer if lemmatizer is not None else load_lemmatizer()
    if lem is None:
        return None                     # 降级：只跑前四项

    for i, (x, y) in enumerate(zip(wb, wa), 1):
        if y.casefold() in fw:
            continue
        # 5) 逐词必须是同一个词的形态（词元共享 ∪ 词干前缀）
        if not _same_word(lem, x, y, lang):
            return (f"第 {i} 个词 {x!r} 与原文 {y!r} 不是同一个词的形态"
                    "（像是换了词，不是还原）")
        # 6) 不得复数化 —— 原文已是词典形（单数），词典形反而变了
        if x.casefold() == y.casefold():
            continue
        if not (_is_known(lem, x, lang) and _is_known(lem, y, lang)):
            continue
        ly = _lemma_of(lem, y, lang).casefold()
        lx = _lemma_of(lem, x, lang).casefold()
        if ly == y.casefold() and lx == y.casefold():
            return (f"第 {i} 个词：原文 {y!r} 已是词典形，却被改成 {x!r}"
                    "（方向搞反了，不该把单数改成复数）")
    return None


def foreign_side(s: Sentence) -> str:
    """判定这句里**外语**（非中文那一侧）在哪个槽位，返回 'src' 或 'tgt'。

    **方向不固定**：`ru-zh` 文件外语在 src，`zh-ru` 在 tgt；而文件名不可信
    （法语语料实测存在内容方向与文件名相反的文件）。所以逐句按汉字占比判，
    汉字少的那侧就是外语侧。三语共用（中文侧无屈折，永远不做还原）。
    """
    return "tgt" if cjk_ratio(s.src) >= cjk_ratio(s.tgt) else "src"


# 旧名保留：俄语那一轮的断言与外部脚本还在用。
russian_side = foreign_side

# 术语列交词典形的语种。中文侧永远不动（无屈折）。
#   ru: 格 -> 主格，且数 -> 单数（ru_v5 起；v4 只还原格）
#   es/fr: 无格，只还原数；名词的性绝不改
DICT_FORM_LANGS = ("es", "fr", "ru")


def norm_types(raw) -> list[str]:
    """规范化 types：小写、只留 10 类白名单、去重保序。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，;/|]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = []
        for x in raw:
            if isinstance(x, str):
                parts.extend(re.split(r"[,，;/|]+", x))
            else:
                parts.append(str(x))
    else:
        parts = [str(raw)]
    seen: list[str] = []
    for p in parts:
        p = p.strip().lower()
        if p in ALLOWED_TYPES and p not in seen:
            seen.append(p)
    # ⚠ `other` 与其余九类**互斥** —— 这是提示词自己定的规则（「Two rules about
    #   types」第 1 条），而定稿 dump 里有 2 条违反了它就这么交出去了
    #   （`游击战术 = tab,other`、`印章 = cul,other`）。`quality.other_mixed` 只计数、
    #   `verify` 只查白名单不查互斥，没有任何一处会拦。
    #   丢掉 `other`、保住信息量更大的那一侧。
    if len(seen) > 1 and "other" in seen:
        seen = [x for x in seen if x != "other"]
    return seen


# --------------------------------------------------------------- 校验

# 交付值首尾不该出现的引号。只看首尾，内部引号是词条自带的标记，不动。
_OUTER_QUOTES = "\"\u201c\u201d\u00ab\u00bb\u2018\u2019'\u300c\u300d\u201e\u2039\u203a"
# 撇号统一到 ASCII：术语库与拼音规范用的都是它。实测 es 7:3 混用、fr 1:47。
_APOSTROPHES = "\u2019\u02bc\u055a\u2032\uff07"


def normalize_delivery_text(s: str) -> str:
    """交付值的标点归一化：撇号统一到 ASCII + 剥掉**整条外层**的引号。

    ⚠ 只给**外语侧**用。中文侧我们对外声明「完全不动」，而且 `verify` 硬要求
      中文侧逐字命中原句 —— 动了它那条断言就会红。

    ⚠ 引号这一条我第一版判错了，记下来：原打算「首尾只要有引号就剥」。
      实测四条里有三条是**配对的内部引号**恰好贴着词尾 ——
      `proyecto "Transmisión Oeste-Este"`、`tunelador "Yuan'an"`、
      `objectif stratégique "en deux étapes"` —— 它们本来就合法，
      单剥一个尾引号只会留下一个落单的引号，比不动更糟。
      所以只剥「整条术语被一对引号裹住、且内部没有别的引号」这一种。
      剩下那条 `“sandwich" de raíces de loto`（弯引号配直引号、不配对）
      **不自动修** —— 错在上游原句（句子原文就是这样），而我们的口径是
      「上游拼错照抄」，猜作者意图正是不该做的事。改由 selfcheck 告警，走 term_qc。
    """
    if not s:
        return s
    for ch in _APOSTROPHES:
        s = s.replace(ch, "'")
    inner = s[1:-1] if len(s) > 2 else ""
    if (len(s) > 2 and s[0] in _OUTER_QUOTES and s[-1] in _OUTER_QUOTES
            and not any(c in _OUTER_QUOTES for c in inner)):
        s = inner.strip()
    return s


def normalize_delivery(rows: list) -> int:
    """把交付值（`term_*_base`，回落时是切片）的标点归一化。返回改了几处。

    原地修改，只动**外语那一侧**：按汉字占比逐行判方向，与 `verify` 同一套判据。
    句中切片 `term_*` 一律不碰 —— 它是逐字锚定的证据，改了就无法溯源。

    做法：归一化后若与切片不同，就写进 `term_*_base`（哪怕本来是空的，
    因为空 = 交付切片本身，而我们要交的是归一化后的值）。
    """
    changed = 0
    for r in rows:
        src = getattr(r, "src_text", "") or ""
        tgt = getattr(r, "tgt_text", "") or ""
        fo_is_src = cjk_ratio(src) < cjk_ratio(tgt)
        side = "src" if fo_is_src else "tgt"
        span = getattr(r, f"term_{side}") or ""
        base = getattr(r, f"term_{side}_base") or ""
        cur = base or span
        new = normalize_delivery_text(cur)
        if new and new != cur:
            setattr(r, f"term_{side}_base", "" if new == span else new)
            changed += 1
    return changed


def unify_dict_forms(rows: list) -> int:
    """同一组内（同中文术语 + 同句中切片）把词典形统一成多数票。返回改了几处。

    `rows` 是**同一个文件**的 TermRow 列表，原地修改。
    只动 `term_*_base`（交付值），句中切片 `term_*` 一律不碰。

    ⚠ 分组键必须带中文术语：只按切片分组会把同形异义合并
      （`осадки` 可以是「降水」也可以是「沉淀物」）。跨文件不统一 ——
      不同讲者不同话题，没有理由强行一致。
    """
    changed = 0
    for side in ("src", "tgt"):
        span_attr, base_attr = f"term_{side}", f"term_{side}_base"
        groups: dict[tuple, list] = {}
        for r in rows:
            span = getattr(r, span_attr, "") or ""
            if not span:
                continue
            other = r.term_tgt if side == "src" else r.term_src
            groups.setdefault((other, span), []).append(r)
        for (_, span), grp in groups.items():
            delivered = [getattr(g, base_attr, "") or span for g in grp]
            if len(set(delivered)) < 2:
                continue
            counts: dict[str, int] = {}
            for d in delivered:
                counts[d] = counts.get(d, 0) + 1
            top = max(counts.values())
            tied = [d for d, c in counts.items() if c == top]
            if len(tied) == 1:
                win = tied[0]
            elif span in tied:
                win = span          # 平票回落到句中形式（既有原则：绝不编）
            else:
                win = next(d for d in delivered if d in tied)   # 出现顺序，保确定性
            for g in grp:
                cur = getattr(g, base_attr, "") or span
                if cur != win:
                    setattr(g, base_attr, "" if win == span else win)
                    changed += 1
    return changed


@dataclass
class TermRow:
    sent_id: object
    src_text: str
    tgt_text: str
    term_src: str            # 句中原始切片（锚定后的真实子串）-> xlsx 第 4 列
    term_tgt: str            # 同上                                    -> xlsx 第 5 列
    types: str               # 逗号连接的多值                          -> xlsx 第 6 列 types
    note: str = ""
    # 词条形式：模型另给的词典形（一格）。**默认空 = 没有词典形**。
    # ⚠ 这两个字段不会直接进 xlsx —— 交付时走下面 `out_*`，它们会先经过
    #   `normalize_delivery`（撇号 / 外层引号归一化）与 `unify_dict_forms`
    #   （同文件同切片多数票）。
    term_src_base: str = ""
    term_tgt_base: str = ""

    @property
    def out_src(self) -> str:
        """写进 xlsx **第 8 列 `term_src_dict`** 的值 = 词条形式。

        有词典形就交词典形，没有（或校验不过回落）就与第 4 列的句中切片相同。
        ⚠ 2026-09-19 之前它写进的是第 4 列 `term_src`；那次改成两列并存 ——
        第 4/5 列必须是逐字的句中切片（转 final.json 时靠它在原句里定位），
        词典形挪到第 8/9 列。**属性语义没变，变的是它落在哪一列。**
        """
        return self.term_src_base or self.term_src

    @property
    def out_tgt(self) -> str:
        """写进 xlsx **第 9 列 `term_tgt_dict`** 的值 = 词条形式。"""
        return self.term_tgt_base or self.term_tgt


@dataclass
class BatchOutcome:
    rows: list[TermRow] = field(default_factory=list)
    n_items: int = 0            # 模型给出的术语条目数
    n_kept: int = 0
    bad_anchor_src: int = 0
    bad_anchor_tgt: int = 0
    bad_types: int = 0
    bad_sent_id: int = 0
    # 模型输出的结构本身畸形（数组元素不是对象 / `terms` 不是数组 / 术语项不是对象）。
    # ⚠ 原先这三处是裸 `continue`：整句术语被丢掉，既不计数也不触发纠正重试。
    bad_shape: int = 0
    # 只是没有 `terms` 键 —— **不算错**。模型对「这句没术语」完全可以只回
    # {"sent_id": 3}。把它当失败会白花一次纠正重试的钱，所以与 bad_shape 分开记。
    no_terms_key: int = 0
    json_failed: bool = False
    # 「**根本解析不出**数组」，与 json_failed 不是一回事：模型给合法的 `[]`
    # 时 json_failed 也是真（那批确实没术语），但 parse_failed 是假。
    # 唯一的用处是决定**要不要撤缓存**，见 run_batch 末尾的 _finish()。
    parse_failed: bool = False
    failures: list[str] = field(default_factory=list)   # 给纠正重试用的说明
    # 词典形还原（`DICT_FORM_LANGS` 里的语种才会动）
    nom_ok: int = 0             # 校验通过、真的交付词典形
    nom_fallback: int = 0       # 校验不过或缺字段，回落到原句切片（**不丢术语**）
    nom_failures: list[str] = field(default_factory=list)   # 回落原因，进报告
    nom_changed: int = 0        # 交付值与句中形式**不同**的条数（真的做了还原）
    nom_plural_kept: int = 0    # 还原后中心词仍是复数 —— 习惯复数，报告里单列供 QC
    nom_reject_same: int = 0    # 校验不过、但词典形与切片**本来相同** -> 交付值无损失
    # 补语（介词后的名词）的数被改了。**指标不是错误**，但 check_lemma_romance
    # 抓不到这一类（实测 5/5 放行），所以必须在报告里让人看见。
    nom_comp_changed: int = 0
    nom_comp_notes: list[str] = field(default_factory=list)


def validate_batch(items: list, batch: list[Sentence],
                   lang: str = "es") -> BatchOutcome:
    """把模型输出校验成可入表的行。

    `lang` 在 `DICT_FORM_LANGS` 里时额外收 `dict_form`（词典形）：判定外语在哪个
    槽位 -> 按语种校验 -> 填 `term_*_base`，不合格则**回落到原句切片并计数，
    绝不丢术语**（召回优先）。中文侧无屈折，`*_base` 永远留空。
    """
    out = BatchOutcome()
    by_pos = {s.pos: s for s in batch}
    if not items:
        out.json_failed = True
        return out
    analyzer = (load_morph() if lang == "ru"
                else load_lemmatizer() if lang in DICT_FORM_LANGS else None)

    for item in items:
        if not isinstance(item, dict):
            out.bad_shape += 1
            out.failures.append(
                f"输出数组里有一个元素不是对象（收到 {type(item).__name__}）"
                "；每个元素必须是 {\"sent_id\": N, \"terms\": [...]}")
            continue
        sid = item.get("sent_id")
        if isinstance(sid, str) and sid.strip().isdigit():
            sid = int(sid.strip())
        s = by_pos.get(sid)
        if s is None:
            out.bad_sent_id += 1
            continue
        terms = item.get("terms")
        if terms is None and "terms" not in item:
            # 没有 terms 键 = 这句没术语。不是错，不触发纠正重试。
            out.no_terms_key += 1
            continue
        if not isinstance(terms, list):
            out.bad_shape += 1
            out.failures.append(
                f"句 {s.pos}: terms 必须是数组（收到 {type(terms).__name__}）")
            continue
        for t in terms:
            if not isinstance(t, dict):
                out.bad_shape += 1
                out.failures.append(
                    f"句 {s.pos}: terms 里有一项不是对象（收到 {type(t).__name__}）")
                continue
            out.n_items += 1
            raw_src = str(t.get("term_src") or "").strip()
            raw_tgt = str(t.get("term_tgt") or "").strip()
            if not raw_src or not raw_tgt:
                out.failures.append(
                    f"句 {s.pos}: term_src/term_tgt 不得为空（收到 {raw_src!r} / {raw_tgt!r}）")
                continue
            anc_src = find_verbatim(raw_src, s.src)
            anc_tgt = find_verbatim(raw_tgt, s.tgt)
            if anc_src is None:
                out.bad_anchor_src += 1
                out.failures.append(
                    f"句 {s.pos}: term_src {raw_src!r} 未在 src 中逐字出现")
                continue
            if anc_tgt is None:
                out.bad_anchor_tgt += 1
                out.failures.append(
                    f"句 {s.pos}: term_tgt {raw_tgt!r} 未在 tgt 中逐字出现")
                continue
            types = norm_types(t.get("types"))
            if not types:
                out.bad_types += 1
                types = ["other"]     # 类型缺失/非法一律降级为 other，不丢术语
            row = TermRow(
                sent_id=s.out_id, src_text=s.src, tgt_text=s.tgt,
                term_src=anc_src, term_tgt=anc_tgt, types=",".join(types),
            )
            if lang in DICT_FORM_LANGS:
                _fill_dict_form(out, row, s, t, lang, analyzer)
            out.rows.append(row)
    out.n_kept = len(out.rows)
    return out


def _fill_dict_form(out: BatchOutcome, row: TermRow, s: Sentence,
                    t: dict, lang: str, analyzer) -> None:
    """把模型给的 `dict_form` 校验后填进 row 的**外语**那一侧。

    失败一律回落（`*_base` 留空 -> 交付时回落到原句切片），只记账不丢行。
    中文侧没有屈折，`*_base` 永远留空。

    ⚠ 兼容旧键 `ru_nom`：ru_v4 的提示词与响应缓存用的是那个键，
    不兼容就等于让已经付过钱的那一轮缓存全部作废。
    """
    side = foreign_side(s)
    anchored = row.term_src if side == "src" else row.term_tgt
    base = str(t.get("dict_form") or t.get("ru_nom") or "").strip()
    if not base:
        out.nom_fallback += 1
        why = f"句 {s.pos}: {anchored!r} 缺 dict_form 字段"
        out.nom_failures.append(why)
        out.failures.append(why + "（每条术语都要给外语侧的词典形）")
        return
    if lang == "ru":
        err = check_nominative(base, anchored, morph=analyzer)
    else:
        err = check_lemma_romance(base, anchored, lang, lemmatizer=analyzer)
    if err:
        out.nom_fallback += 1
        # ⚠ 词典形与切片本来相同时，回落交付的是同一个字符串 —— **交付值无损失**。
        #   单列出来，免得把它算进「真实损失」（2026-09-17 我就这么读错过一次）。
        if base.strip() == (anchored or "").strip():
            out.nom_reject_same += 1
        why = f"句 {s.pos}: dict_form {base!r} 对 {anchored!r} 不合格 —— {err}"
        out.nom_failures.append(why)
        out.failures.append(why)
        return
    out.nom_ok += 1
    if base != anchored:
        out.nom_changed += 1
    if _plural_kept(base, lang, analyzer):
        out.nom_plural_kept += 1
    _cc = complement_num_changed(base, anchored, lang)
    if _cc:
        out.nom_comp_changed += 1
        out.nom_comp_notes.append(f"句 {s.pos}: {base!r} ← {anchored!r} —— {_cc}")
    if side == "src":
        row.term_src_base = base
    else:
        row.term_tgt_base = base


_COMP_PLURAL_END = ("s", "x")
_ELISION = ("d'", "l'", "d’", "l’")

# ⚠ 只认**真正的介词**，不要把并列连词算进来。`et` / `y` 后面那个词是与中心词
#   配合的并列成分（`porcelaines bleues et blanches` -> `porcelaine bleue et
#   blanche` 是对的），不是补语。用 _FUNCTION_WORDS 会把这一类误报成
#   「补语的数被改了」—— 2026-09-18 跑 fr_v6 探针时实测到。
_COMP_PREPS = {
    "es": {"de", "del", "en", "a", "al", "con", "por", "para", "sin", "sobre"},
    "fr": {"de", "du", "des", "d'", "d’", "en", "à", "au", "aux", "sur", "sans"},
}


def complement_num_changed(base: str, span: str, lang: str) -> str | None:
    """补语（介词后面的名词）的数变了 -> 返回一句说明，供**人工核**；否则 None。

    不是门槛，是指标。理由见本文件顶部与 README：
      * 补语的数不随中心词配合，是词条自带的词汇属性，携带语义；
      * `check_lemma_romance` 抓不到这类错（实测 5/5 全放行），只能靠人看；
      * 法语老师 2026-09-18 的口径：补语表类别用单数（`forme d'entreprise`），
        表构成实体用复数（`groupement d'entreprises`、`immeuble de bureaux`）。
        哪一类是词汇知识，程序判不了。

    只对 es/fr 生效。
    ⚠ 原注释写「俄语的从属成分已由 check_nominative 覆盖」，**那是错的**：
      check_nominative 的判据 4（中心组必须主格）只遍历 head_group，从属成分只受
      判据 2（词干前缀）与判据 5（共享词元）约束，而属格复数改成属格单数
      **词干相同、词元相同**，两条都放行。所以俄语从属成分的「数」目前没人管。
      实测这个缺口在现有样本里未造成损害（2026-09-18 的临时检测器 38 条全是假阳性，
      head_group 把真正的中心名词切进了「从属」），故本轮只改注释、不动逻辑。
    """
    if lang not in ("es", "fr"):
        return None
    wb, wa = nom_words(base), nom_words(span)
    if not wb or len(wb) != len(wa):
        return None
    preps = _COMP_PREPS.get(lang, frozenset())
    for i in range(1, len(wb)):
        tok_b, tok_a = wb[i], wa[i]
        prev_a = wa[i - 1].casefold()
        # 补语的两种形态：前一个词是介词（`de bureaux`），
        # 或者省音直接粘在词上（`d'entreprises` 被 nom_words 切成一个词）
        if not (prev_a in preps or tok_a.casefold().startswith(_ELISION)):
            continue
        x, y = tok_b.casefold(), tok_a.casefold()
        if x == y:
            continue
        xp = x.endswith(_COMP_PLURAL_END)
        yp = y.endswith(_COMP_PLURAL_END)
        if yp and not xp:
            return f"补语 {tok_a!r} -> {tok_b!r}（复数改成了单数）"
        if xp and not yp:
            return f"补语 {tok_a!r} -> {tok_b!r}（单数改成了复数）"
    return None


def _plural_kept(base: str, lang: str, analyzer) -> bool:
    """交付的词典形里，中心词是否仍是复数。

    **不是门槛，只是报告指标。** IATE 手册允许「习惯复数」（`derechos humanos`、
    `совместные учения`），程序判不了「习惯」，所以这些条目单列出来给人抽检。
    """
    if analyzer is None:
        return False
    ws = nom_words(base)
    if not ws:
        return False
    try:
        if lang == "ru":
            head = head_group(ws, analyzer)[-1:]
            if not head or not _has_cyr(head[0]):
                return False
            ps = analyzer.parse(head[0])
            return bool(ps) and all(p.tag.number == "plur" for p in ps)
        # 西/法：第一个非功能词当中心词（`luces de neón` 的 `luces`）
        fw = _FUNCTION_WORDS.get(lang, set())
        head = next((w for w in ws if w.casefold() not in fw), "")
        if not head:
            return False
        # 专名**显式排除**：`Naciones Unidas` 这类保留复数是理所当然的，报出来只是噪声。
        # ⚠ 必须写成显式规则 —— simplemma 对大写词的行为不一致（实测 `Naciones`
        #   原样返回、`Estados` 却还原成 `estado`），指望它自然排除会得到随机结果。
        if head[:1].isupper():
            return False
        if not _is_known(analyzer, head, lang):
            return False
        # 判「是复数」而**不是**判「词元与原词不同」。
        #
        # ⚠ 2026-09-18 踩过并修：原先写的是 `lemma != head`，结果 651 条里报出 15 条
        #   「仍是复数」，逐条看**大部分根本不是复数** —— simplemma 改动一个词还有
        #   两个与数无关的理由：给动词词元（`estado`->`estar`、`desplazado`->`desplazar`）
        #   和抹平性（`antigua`->`antiguo`、`abrumadora`->`abrumador`）。
        #   这个指标是给老师判「习惯复数」用的，报错了就是误导。
        # 现在的判法：词尾是 -s/-x 而词元的词尾不是。罗曼语的复数就是加 -s（法语
        # 另有 -x），所以这条既抓得住 `luces`->`luz`、`travaux`->`travail` 这类不规则，
        # 也放得过单复同形的 `crisis`、`país`、`pays`（它们的词元自己也以 s 结尾）。
        h = head.casefold()
        if not h.endswith(("s", "x")):
            return False
        return not _lemma_of(analyzer, head, lang).casefold().endswith(("s", "x"))
    except Exception:       # noqa: BLE001 —— 指标算不出来不该拖垮整轮
        return False


CORRECTION_TEMPLATE = (
    "Your previous answer contained terms that do NOT appear verbatim in the "
    "given sentences. Every term_src must be a contiguous substring of that "
    "sentence's src, and every term_tgt a contiguous substring of its tgt. "
    "Copy them character-for-character; do not translate, inflect, or complete them.\n\n"
    "Problems found:\n{problems}\n\n"
    "Re-output the COMPLETE corrected JSON array for the same sentences. "
    "Drop any term you cannot anchor verbatim. Output ONLY the JSON array."
)

# 词典形的纠正重试追加段，**按语种取**。
# ⚠ 纠正模板的文本不进缓存键，所以无条件改它会静默改变别的语种的重试内容 ——
#   必须按 lang 分派，非本语种的一个字都不动。
DICT_FORM_CORRECTION = {
    "ru": (
        "\n\nSome problems above are about `dict_form`, the dictionary form of the "
        "FOREIGN side of the pair (never the Chinese side). Rules for it: "
        "restore the Russian side to the NOMINATIVE SINGULAR; the head word goes "
        "nominative and its modifiers agree; a plural head becomes singular "
        "(`диких кабанов` -> `дикий кабан`) UNLESS the term is normally used only "
        "in the plural (`совместные учения`, `права человека`) or is a proper name "
        "or an abbreviation; a subordinate noun keeps its own case (`империю лжи` "
        "-> `империя лжи`); the number of words never changes; nothing is "
        "translated; `ё` spelling is preserved. If the span is already the "
        "dictionary form, repeat it unchanged. Keep `term_src`/`term_tgt` verbatim "
        "as before — only fix `dict_form`."
    ),
    "es": (
        "\n\nSome problems above are about `dict_form`, the dictionary form of the "
        "FOREIGN side of the pair (never the Chinese side). Rules for it: "
        "restore the Spanish side to the DICTIONARY FORM: the head noun goes "
        "SINGULAR and its adjectives agree with it (`empresas privadas` -> "
        "`empresa privada`), UNLESS the term is normally used in the plural "
        "(`derechos humanos`, `fuerzas armadas`) or is a proper name "
        "(`Estados Unidos`) or an abbreviation. NEVER change the gender of a noun: "
        "`empresa privada`, not `empresa privado`. Articles and prepositions inside "
        "the span stay exactly as they are (`luces de neón` -> `luz de neón`). "
        "The number of words never changes; nothing is translated. If the span is "
        "already the dictionary form, repeat it unchanged. Keep "
        "`term_src`/`term_tgt` verbatim as before — only fix `dict_form`."
    ),
    "fr": (
        "\n\nSome problems above are about `dict_form`, the dictionary form of the "
        "FOREIGN side of the pair (never the Chinese side). Rules for it: "
        "restore the French side to the DICTIONARY FORM: the head noun goes "
        "SINGULAR and its adjectives agree with it (`forces terroristes` -> "
        "`force terroriste`), UNLESS the term is normally used in the plural "
        "(`droits de l'homme`, `arts et métiers traditionnels`) or is a proper name "
        "(`Nations unies`) or an abbreviation. NEVER change the gender of a noun: "
        "`nouvelle forme`, not `nouveau forme`. Articles, prepositions and elisions "
        "inside the span stay exactly as they are (`dettes en souffrance` -> "
        "`dette en souffrance`). The number of words never changes; nothing is "
        "translated. If the span is already the dictionary form, repeat it "
        "unchanged. Keep `term_src`/`term_tgt` verbatim as before — only fix "
        "`dict_form`."
    ),
}

# 旧名保留：俄语那一轮的断言引用了它。
NOM_CORRECTION_EXTRA = DICT_FORM_CORRECTION["ru"]


async def run_batch(client: LLMClient, system: str, user_template: str,
                    batch: list[Sentence], file_md5: str, batch_idx: int,
                    correct_retry: bool = True,
                    lang: str = "es") -> tuple[BatchOutcome, list[CallResult]]:
    """跑一个批次，必要时做一次纠正重试。返回 (结果, 本批所有调用)。"""
    calls: list[CallResult] = []
    messages = build_messages(system, user_template, batch)
    bkey = batch_key(batch)
    r = await client.call(messages, file_md5, batch_idx, bkey)
    calls.append(r)
    if not r.ok:
        oc = BatchOutcome(json_failed=True)
        oc.failures.append(f"API 失败: {r.error}")
        return oc, calls

    def _finish(outcome: BatchOutcome) -> tuple[BatchOutcome, list[CallResult]]:
        """收尾：**解析不出来的响应必须从缓存里撤掉**。

        不撤会怎样：那种响应 `refused` / `truncated` 都是假（ok=True、content
        非空、finish_reason 正常），于是 `llm.call()` 的 `if not r.refused and
        not r.truncated` 判真、**照落盘**；纠正重试的响应（idx 10_000+）同样落盘。
        重跑同一条命令时首轮命中缓存 -> 同样的 content -> parse_json_array 是纯
        确定性函数 -> 仍然失败 -> 纠正重试也命中缓存 -> 同样的失败。
        **一次 API 都不发，结果逐字节相同。** 而闸门提示写的是「请重跑同一条
        命令补齐失败批」—— 对这一类批那是句空话。同一处伤口第三次
        （09-16 refused、09-19 truncated、09-19 夜 parse 失败）。

        只在**最终结果**解析不出来时撤：若纠正重试救回来了，两条缓存都留着，
        否则下次重跑要为已经成功的那次重新付费。
        """
        if outcome.parse_failed:
            client.invalidate(file_md5, batch_idx, bkey)
            client.invalidate(file_md5, 10_000 + batch_idx, bkey)
        return outcome, calls

    items, _parsed = parse_json_array_ex(r.content)
    oc = validate_batch(items, batch, lang=lang)
    oc.parse_failed = not _parsed

    need_retry = correct_retry and (oc.failures or (oc.json_failed and r.content.strip()))
    if need_retry:
        problems = "\n".join(f"- {p}" for p in oc.failures[:25]) or "- output was not a valid JSON array"
        tmpl = CORRECTION_TEMPLATE.format(problems=problems)
        if lang in DICT_FORM_CORRECTION:
            tmpl += DICT_FORM_CORRECTION[lang]
        retry_msgs = messages + [
            # ⚠ 上限从 8000 提到 20000：实测三语 bake-off 的响应 max 7,125 字符，
            #   但那只有 44~59 个样本；全量 18,605 批必然探出更长的。回抄被截断
            #   = 把一个非法 JSON 当上下文喂给纠正重试，钱花了还换不回更好的答案。
            {"role": "assistant", "content": r.content[:20000]},
            {"role": "user", "content": tmpl},
        ]
        # 纠正重试用不同的 batch_idx 命名空间，避免覆盖首轮缓存
        r2 = await client.call(retry_msgs, file_md5, 10_000 + batch_idx, bkey)
        calls.append(r2)
        if r2.ok:
            items2, _parsed2 = parse_json_array_ex(r2.content)
            oc2 = validate_batch(items2, batch, lang=lang)
            oc2.parse_failed = not _parsed2
            # 什么算「更好」：抽到的术语更多，或术语数相同但问题更少。
            # ⚠ 2026-09-16 修的 bug：原条件写成
            #     oc2.n_kept >= oc.n_kept and len(oc2.failures) < len(oc.failures)
            #   首轮 JSON 解析失败时 oc.failures 是空的（validate_batch 直接返回），
            #   于是 0 < 0 为假 —— 重试即使把 JSON 完全修好也会被丢弃，白扔召回。
            better = (oc2.n_kept > oc.n_kept
                      or (oc2.n_kept == oc.n_kept
                          and len(oc2.failures) < len(oc.failures)))
            if better:
                orig_failures = oc.failures[:]     # 保留首轮问题记录供报告
                oc2.json_failed = oc2.n_kept == 0
                oc2.failures = orig_failures
                return _finish(oc2)
    return _finish(oc)
