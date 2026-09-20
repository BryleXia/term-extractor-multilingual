"""语料读取层：把 zip 里的 align.qc.json 变成统一的句子列表。

本模块承担计划 §2 的全部 15 类异常适配。设计原则：
  * 只依赖「句子列表」这一件事，顶层字段一律不当必需项；
  * 术语锚定按 src/tgt **槽位**做，不关心哪边是中文（§2 行 12/13）；
  * 内部一律用数组位置（1..N）定位句子，送给模型的 id 就是位置；
    原 sent_id 只在「唯一且非 0」时才写进 xlsx（§2 行 8/9）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import CORPUS_DIR

# ---------------------------------------------------------------- 文件名规则

# 上游把 align 拼错的三种写法（法语语料实测，西语没有）。
# 不归一化的话按 HTML 规则会产出 `..._algin.qc_term.xlsx` 这种带错别字
# 又带多余 `.qc` 的名字。用户已定：我们纠正拼写，输出干净名字，
# 并在交付说明里给下游工具方逐条映射表（这是与 HTML 的第三处有意差异）。
#
# ⚠ 只改「拼错的 align」。`_align.json`（缺 `.qc`，法语有 23 个）**不动** ——
#   align 拼写是对的，少 `.qc` 是上游流程差异不是错字。
_NAME_FIXES = (
    ("_algin.qc.json", "_align.qc.json"),
    ("_aglin.qc.json", "_align.qc.json"),
    ("_align.qc..json", "_align.qc.json"),
)

# ⚠ 第 4 类命名缺陷（俄语全量，2026-09-18 实测，西语法语都没有）：
#   `..._align.qc (1).json` —— 浏览器重复下载的后缀
#   `..._align.qc .json`    —— `.qc` 后一个游离空格
#   两者都不以 `_align.qc.json` 结尾，会掉进 replace_out_name 的通用分支，
#   产出 `..._align.qc (1)_term.xlsx` 这种名字。已核实这 3 个文件
#   （poli_0007_seg001 / 0023_seg001 / 0023_seg002，共 316 句）**都没有规范名兄弟**，
#   是真内容不是重复下载，不能丢。用正则而不是穷举 (1)(2)(3)。
_DUP_SUFFIX = re.compile(r"\s*\(\d+\)(?=\.jsonl$|\.json$)")
_STRAY_SPACE = re.compile(r"\s+(?=\.jsonl$|\.json$)")


def normalize_basename(name: str) -> tuple[str, str | None]:
    """纠正上游文件名里的 align 拼写错误、重复下载后缀与游离空格。

    返回 (归一化后的名字, 说明或 None)。说明会进 CorpusFile.notes，
    并在交付说明里给下游工具方逐条映射表。
    """
    fixed = _DUP_SUFFIX.sub("", name)
    fixed = _STRAY_SPACE.sub("", fixed)
    for bad, good in _NAME_FIXES:
        if fixed.endswith(bad):
            fixed = fixed[: -len(bad)] + good
            break
    if fixed != name:
        return fixed, f"文件名拼写已纠正：{name} -> {fixed}"
    return name, None


def out_name_for(basename: str) -> tuple[str, str | None]:
    """先归一化拼写，再套 HTML 的 replaceOutName。这是产出物真正用的那个函数。"""
    fixed, note = normalize_basename(basename)
    return replace_out_name(fixed), note


def replace_out_name(name: str) -> str:
    """逐字移植 HTML 工具的 replaceOutName()（计划 §1）。

    **保持与 HTML 逐字一致，不要在这里加归一化** —— 归一化在 normalize_basename()，
    tests.py 与 verify.py 都靠本函数复核我们没偷偷改她的规则。
    """
    if name.endswith("align.qc.json"):
        return name[: -len("align.qc.json")] + "term.xlsx"
    if name.endswith(".jsonl") or name.endswith(".json"):
        return re.sub(r"\.(jsonl|json)$", "_term.xlsx", name)
    return name + "_term.xlsx"


def _decode_member(info: zipfile.ZipInfo) -> str:
    """zip 里的中文名可能是 GBK（未置 UTF-8 标志位时 Python 按 cp437 解）。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


# ---------------------------------------------------------------- JSON 语法修复

def repair_json(text: str, max_fix: int = 40) -> tuple[object, list[str]]:
    """修上游导出脚本写坏的 JSON。返回 (解析结果或 None, 修复动作清单)。

    法语语料实测：6 个 conf_poli 文件在第 7 行 `"session_no": "0035"` 后**缺一个逗号**，
    其中 `zh-fr_conf_poli_0011_seg002` 还多一个尾逗号。这些文件 `json.loads` 直接抛错，
    退回贪婪正则 `[...]` 只能救回句子数组（丢顶层 meta），最坏的那个连正则也救不回来
    —— **下游工具方的 HTML 工具用的是同一个正则兜底，所以它会整文件丢掉那 123 句。**

    做法是**错误驱动的定点修复**，不是通用 JSON 修补器：只认两种错，
    改一处就重新 parse，其他错一律放弃并记账。实测对 612 个法语条目里的
    606 个零改动，只动了该动的 6 个。
    """
    fixes: list[str] = []
    for _ in range(max_fix):
        try:
            return json.loads(text), fixes
        except json.JSONDecodeError as e:
            if e.msg.startswith("Expecting ',' delimiter"):
                i = e.pos - 1
                while i >= 0 and text[i].isspace():
                    i -= 1
                if i < 0:
                    return None, fixes + ["无法定位缺失的逗号"]
                text = text[: i + 1] + "," + text[i + 1:]
                fixes.append(f"第 {e.lineno} 行补一个缺失的逗号")
            elif "trailing comma" in e.msg:
                i = text.rfind(",", 0, e.pos + 1)
                if i < 0:
                    return None, fixes + ["无法定位多余的逗号"]
                text = text[:i] + text[i + 1:]
                fixes.append(f"第 {e.lineno} 行删一个多余的逗号")
            else:
                return None, fixes + [f"无法自动修复的 JSON 错误：{e.msg}（第 {e.lineno} 行）"]
    return None, fixes + [f"修复次数超过 {max_fix} 次，放弃"]


# ---------------------------------------------------------------- 文本归一化

def norm_text(x) -> str:
    """把任意形态的文本单元收成字符串。

    覆盖 §2 行 10（空/None）、行 11（int）。嵌套形态照 HTML 的 get_text：
    对象取 .text 或 .raw。
    """
    if x is None:
        return ""
    if isinstance(x, dict):
        x = x.get("text", None)
        if x is None:
            x = ""
    if isinstance(x, (list, tuple)):
        x = " ".join(norm_text(i) for i in x)
    if not isinstance(x, str):
        x = str(x)
    return x.strip()


def is_meaningful(s: str) -> bool:
    """有没有字母/汉字/西里尔字母。纯数字、纯标点、空串一律视为空（§2 行 11）。"""
    return any(ch.isalpha() for ch in s)


def cjk_ratio(s: str) -> float:
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars if "一" <= c <= "鿿")
    return cjk / len(chars)


def fold(s: str) -> str:
    """校验用的归一化：NFC + 折叠空白 + 拉丁大小写不敏感（计划 §7 extract）。"""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


# ---------------------------------------------------------------- 数据结构

@dataclass
class Sentence:
    pos: int                # 1..N 数组位置，内部唯一 id，送给模型的就是这个
    out_id: object          # 写进 xlsx 的 sent_id（原值或 pos）
    src: str
    tgt: str

    @property
    def usable(self) -> bool:
        """两侧都有实义内容才值得花 token（§2 行 10）。"""
        return is_meaningful(self.src) and is_meaningful(self.tgt)


@dataclass
class CorpusFile:
    zip_name: str
    member: str                       # zip 内完整路径
    basename: str                     # 扁平文件名
    out_name: str                     # xlsx 名（replace_out_name 的结果）
    md5: str                          # 原始字节 md5，用于去重（§2 行 14）
    schema: str                       # standard / subtitles / alignment_data / bare_array
    sentences: list[Sentence] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # scene/domain/pair/align_id 原样
    notes: list[str] = field(default_factory=list)  # 适配过程中的异常记录
    shifted_recovered: int = 0        # 字段错位被还原的句数（法语语料，见 _recover_shifted）

    @property
    def usable_sentences(self) -> list[Sentence]:
        return [s for s in self.sentences if s.usable]

    @property
    def stem_key(self) -> str:
        """去掉 align.qc.json 后缀的主干，用于分层与报告。"""
        n = self.basename
        for suf in ("_align.qc.json", ".align.qc.json"):
            if n.endswith(suf):
                return n[: -len(suf)]
        return re.sub(r"\.(jsonl|json|xlsx)$", "", n)

    def layer(self) -> str:
        """scene/domain 分层键，优先用文件名（顶层字段有拼写错误，§2 行 15）。"""
        parts = self.stem_key.split("_")
        if len(parts) >= 3:
            return f"{parts[1]}/{parts[2]}"
        return self.meta.get("scene", "?") + "/" + self.meta.get("domain", "?")

    def name_direction(self) -> str:
        parts = self.stem_key.split("_")
        return parts[0] if parts else "?"

    def content_direction(self) -> str:
        """逐句按 CJK 占比判定 src 槽位实际是哪种语言（§2 行 13，仅供报告）。"""
        pairs = self.usable_sentences
        if not pairs:
            return "?"
        src_cjk = sum(1 for s in pairs if cjk_ratio(s.src) > 0.3)
        tgt_cjk = sum(1 for s in pairs if cjk_ratio(s.tgt) > 0.3)
        if src_cjk > tgt_cjk:
            return "zh-first"
        if tgt_cjk > src_cjk:
            return "foreign-first"
        return "unclear"


# ---------------------------------------------------------------- 句子抽取

_SENT_LIST_KEYS = ("sentences", "subtitles", "alignment_data", "aligned", "data")
_ID_KEYS = ("sent_id", "sen_id", "id", "index", "idx")
_SRC_KEYS = ("src", "source", "src_text", "source_text", "zh", "chinese")
_TGT_KEYS = ("tgt", "target", "tgt_text", "target_text", "trans", "translation")


def _find_sentence_list(data) -> tuple[list, str]:
    """找出句子数组，返回 (数组, schema 标签)。覆盖 §2 行 5/6/7。"""
    if isinstance(data, list):
        return data, "bare_array"
    if isinstance(data, dict):
        for k in _SENT_LIST_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                return v, "standard" if k == "sentences" else k
        # 兜底：找第一个「元素是 dict 的数组」
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v, f"guessed:{k}"
    return [], "unknown"


def _pick(item: dict, keys) -> object:
    for k in keys:
        if k in item:
            return item[k]
    return None


# SRT 时间轴行的特征：`00:00:00,100 --> 00:00:08,100`（逗号或点都见过）
_SRT_TS = re.compile(r"^\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->")


def looks_like_srt_ts(s: str) -> bool:
    return bool(_SRT_TS.match(s.strip()))


def _recover_shifted(item: dict) -> tuple[str, str] | None:
    """法语语料的字段整体错位还原（11 个文件 / 1,528 句）。

    上游导出脚本把 SRT 的字段整列串位了，两种形态：

      形态 1（5 个文件，629 句）—— 外文在 tgt.text
        src: {start_ms: "1",            text: "00:00:00,100 --> 00:00:08,100"}
        tgt: {start_ms: "<中文正文>",   text: "<法文正文>"}

      形态 2（6 个文件，899 句）—— 两侧 text 都是时间轴，外文掉进 notes
        src: {start_ms: "1",            text: "00:00:01,000 --> 00:00:05,266"}
        tgt: {start_ms: "<中文正文>",   text: "00:00:00,000 --> 00:00:05,270"}
        notes: "<法文正文>"

    不还原的后果：`src` 是时间轴 -> `is_meaningful` 判假 -> 整句被过滤，
    **这 11 个文件一个术语都出不来、一份 xlsx 都不产出**。下游那套 HTML 工具更糟：
    她的 `get_text` 取 `x.text`，会把时间轴当中文喂给模型，产出垃圾术语。

    返回 (中文, 外文) 或 None（判不出来就别硬猜，交回原样并记账）。
    """
    src, tgt = item.get("src"), item.get("tgt")
    if not isinstance(src, dict) or not isinstance(tgt, dict):
        return None
    if not looks_like_srt_ts(norm_text(src.get("text"))):
        return None
    zh = norm_text(tgt.get("start_ms"))            # 中文被挤到了 start_ms
    tgt_text = norm_text(tgt.get("text"))
    foreign = norm_text(item.get("notes")) if looks_like_srt_ts(tgt_text) else tgt_text
    # 两侧都得是实义文本才敢采用
    if is_meaningful(zh) and is_meaningful(foreign):
        return zh, foreign
    return None


def _extract_pair(item) -> tuple[str, str, str | None]:
    """从一个句子条目里取出 (src, tgt, 异常标记)。兼容嵌套与扁平两种形态。"""
    if not isinstance(item, dict):
        return "", "", None
    src = norm_text(_pick(item, _SRC_KEYS))
    tgt = norm_text(_pick(item, _TGT_KEYS))
    if looks_like_srt_ts(src):
        rec = _recover_shifted(item)
        if rec:
            return rec[0], rec[1], "shifted_recovered"
        return src, tgt, "shifted_unrecoverable"
    return src, tgt, None


def _decide_out_ids(raw_ids: list, n: int) -> tuple[list, str | None]:
    """§2 行 8/9 的通用规则：原 id 唯一且非 0 才沿用，否则按位置重编 1..N。"""
    ok = (
        len(raw_ids) == n
        and all(isinstance(i, int) and i != 0 for i in raw_ids)
        and len(set(raw_ids)) == n
    )
    if ok:
        return raw_ids, None
    reason = "sent_id 缺失或非整数"
    if len(raw_ids) == n:
        if any(i == 0 for i in raw_ids if isinstance(i, int)):
            reason = "sent_id 含 0"
        elif len(set(raw_ids)) != n:
            reason = "sent_id 有重复"
    return list(range(1, n + 1)), f"已按数组位置重编 1..{n}（原因：{reason}）"


def parse_file(raw: bytes, member: str, zip_name: str) -> CorpusFile:
    """把一个文件的原始字节解析成 CorpusFile。"""
    basename = Path(member).name
    out_name, name_note = out_name_for(basename)
    cf = CorpusFile(
        zip_name=zip_name,
        member=member,
        basename=basename,
        out_name=out_name,
        md5=hashlib.md5(raw).hexdigest(),
        schema="unknown",
    )
    if name_note:
        cf.notes.append(name_note)

    text = raw.decode("utf-8-sig", errors="replace").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # jsonl？照 HTML 的 parseSentences 逻辑退一步
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if lines and lines[0].startswith("{") and len(lines) > 1:
                try:
                    data = [json.loads(l) for l in lines]
                except json.JSONDecodeError:
                    data = None
            # 定点修语法错误。放在贪婪正则之前 —— 正则只能捞出句子数组、
            # 会丢掉顶层 meta，而且有的文件它根本救不回来（HTML 就是死在这里）。
            if data is None:
                data, fixes = repair_json(text)
                if fixes:
                    cf.notes.append("JSON 语法有错，已定点修复：" + "；".join(fixes))
            if data is None:
                m = re.search(r"\[[\s\S]*\]", text)
                if m:
                    try:
                        data = json.loads(m.group(0))
                        cf.notes.append("JSON 修不好，退回正则捞句子数组（顶层 meta 丢失）")
                    except json.JSONDecodeError:
                        data = None
    if data is None:
        cf.notes.append("JSON 解析失败，整文件跳过")
        return cf

    if isinstance(data, dict):
        for k in ("align_id", "scene", "domain", "pair", "session_no", "seg_no"):
            if k in data:
                cf.meta[k] = data[k]
        # 异种 schema 的元信息藏在子对象里
        for holder in ("metadata", "project_info", "info"):
            sub = data.get(holder)
            if isinstance(sub, dict):
                for k in ("align_id", "scene", "domain", "pair"):
                    cf.meta.setdefault(k, sub.get(k)) if k in sub else None

    items, schema = _find_sentence_list(data)
    cf.schema = schema
    if schema not in ("standard", "bare_array"):
        cf.notes.append(f"异种 schema：句子数组在 '{schema}' 键下（HTML 工具会整文件跳过）")
    if not items:
        cf.notes.append("未找到句子数组，0 句")
        return cf

    raw_ids = [_pick(i, _ID_KEYS) if isinstance(i, dict) else None for i in items]
    out_ids, renote = _decide_out_ids(raw_ids, len(items))
    if renote:
        cf.notes.append(renote)

    empty = 0
    recovered = 0
    unrecoverable = 0
    for pos, (item, oid) in enumerate(zip(items, out_ids), start=1):
        src, tgt, flag = _extract_pair(item)
        if flag == "shifted_recovered":
            recovered += 1
        elif flag == "shifted_unrecoverable":
            unrecoverable += 1
        s = Sentence(pos=pos, out_id=oid, src=src, tgt=tgt)
        if not s.usable:
            empty += 1
        cf.sentences.append(s)
    if recovered:
        cf.shifted_recovered = recovered
        cf.notes.append(
            f"{recovered} 句字段整体错位（时间轴写进了 text）已还原；"
            "**上游对齐质量本身可疑，建议复查此文件**")
    if unrecoverable:
        cf.notes.append(f"{unrecoverable} 句字段错位且无法还原，不送模型")
    if empty:
        cf.notes.append(f"{empty} 句因单侧或双侧为空/纯数字而不送模型")
    return cf


# ---------------------------------------------------------------- 集合级加载

_SKIP_SUFFIX = (".xlsx", ".xls", ".csv", ".md", ".txt", ".srt", ".wav", ".m4a")


def load_zip(zip_path: Path) -> tuple[list[CorpusFile], list[str]]:
    """读一个 zip，返回 (文件列表, 跳过说明)。"""
    files: list[CorpusFile] = []
    skipped: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            member = _decode_member(info)
            base = Path(member).name
            if base.startswith(("__MACOSX", "._")) or "/__MACOSX" in member:
                continue
            if base.lower().endswith(_SKIP_SUFFIX):
                skipped.append(f"{zip_path.name}:{member}（非 JSON，跳过；HTML 同样跳过）")
                continue
            if not base.lower().endswith((".json", ".jsonl")):
                skipped.append(f"{zip_path.name}:{member}（未知扩展名，跳过）")
                continue
            files.append(parse_file(z.read(info), member, zip_path.name))
    return files, skipped


LOOSE_NAME = "(散装)"        # 散装文件在报告里的来源名


def load_loose(paths: list[Path]) -> tuple[list[CorpusFile], list[str]]:
    """读目录里**散装**的 json/jsonl（不在 zip 里），口径与 load_zip 完全一致。

    为什么需要这条路（俄语语料实测）：俄语测试语料交来的是 10 个散装 `.json`，
    不是 zip。`load_corpus` 原先只 `glob("*.zip")`，一个文件都读不到、报 0 句，
    而且**不报错** —— 典型的静默失败。
    """
    files: list[CorpusFile] = []
    skipped: list[str] = []
    for path in paths:
        base = path.name
        if base.startswith(("__MACOSX", "._")):
            continue
        if base.lower().endswith(_SKIP_SUFFIX):
            skipped.append(f"{LOOSE_NAME}:{base}（非 JSON，跳过；HTML 同样跳过）")
            continue
        if not base.lower().endswith((".json", ".jsonl")):
            skipped.append(f"{LOOSE_NAME}:{base}（未知扩展名，跳过）")
            continue
        files.append(parse_file(path.read_bytes(), base, LOOSE_NAME))
    return files, skipped


def loose_paths(corpus_dir: Path) -> list[Path]:
    """目录下的散装 json/jsonl（不含 zip 内的）。"""
    return sorted(q for pat in ("*.json", "*.jsonl") for q in corpus_dir.glob(pat))


def zip_basenames(zip_path: Path) -> set[str]:
    """只读目录项，取 zip 里的 basename 集合（不解压内容）。"""
    with zipfile.ZipFile(zip_path) as z:
        return {Path(_decode_member(i)).name for i in z.infolist() if not i.is_dir()}


def load_corpus(corpus_dir: Path | None = None) -> tuple[list[CorpusFile], list[str]]:
    """读全部 zip **与散装 json/jsonl**，**跨来源重名只保留一份**。

    ⚠ 两种载体都要支持：西语/法语交来的是 zip，**俄语交来的是 10 个散装 `.json`**。
    只 glob zip 的话俄语会读出 0 个文件且不报错（静默失败）。

    为什么必须去重（法语语料实测）：10 个 zip 共 612 个条目，只有 555 个唯一
    basename —— 57 个重名。不去重的话同一份内容**付两次钱**，而且两份的
    `out_name` 相同，第二份 xlsx 会**直接覆盖**第一份。

    留哪一份：**basename 集合是超集的那个 zip**。法语实测包含关系很干净
    （`13个文件 ⊂ 51个文件`、`44个文件 ⊂ 87个文件`），而且逐句 diff 证实超集那份
    是校对后的版本（`以船为家` vs 错字 `以船为驾`、`贬谪…流放` vs `贬折…留放`）。
    没有包含关系时退到「条目多的 / 修改时间晚的 / 名字靠后的」，并在报告里
    标成「按启发式选择」提醒人工确认。

    ⚠ **绝不按 session 号去重。** `révisé` zip 里 87 个 `fr-zh_conf_poli_*` 与另一个 zip 的
    `zh-fr_conf_poli_*` 有 47 个 session 号重合，名字又叫「修订版」，很像同一批素材
    换方向重做。逐句比过：**文本重合率 0%**，是完全不同的讲话。按 session 去重会白扔 87 个真文件。
    """
    corpus_dir = corpus_dir or CORPUS_DIR
    zips = sorted(corpus_dir.glob("*.zip"))
    names: dict[Path, set[str]] = {zp: zip_basenames(zp) for zp in zips}
    # 排名越大越优先：条目数 -> 修改时间 -> 名字
    rank = {zp: (len(names[zp]), zp.stat().st_mtime, zp.name) for zp in zips}

    # 散装 json/jsonl 当成一个虚拟来源，走同一条去重/冲突链路（俄语语料就是散装的）。
    loose = loose_paths(corpus_dir)
    loose_key = corpus_dir / LOOSE_NAME          # 只作字典键，不落地
    sources: list[Path] = list(zips)
    if loose:
        names[loose_key] = {q.name for q in loose}
        rank[loose_key] = (len(loose), max(q.stat().st_mtime for q in loose), LOOSE_NAME)
        sources.append(loose_key)

    kept: dict[str, CorpusFile] = {}
    all_skipped: list[str] = []
    conflicts: list[str] = []
    for zp in sources:
        fs, sk = load_loose(loose) if zp == loose_key else load_zip(zp)
        all_skipped.extend(sk)
        for f in fs:
            old_f = kept.get(f.basename)
            if old_f is None:
                kept[f.basename] = f
                continue
            old_zp = corpus_dir / old_f.zip_name
            win_new = rank[zp] > rank.get(old_zp, ())
            loser, winner = (old_f, f) if win_new else (f, old_f)
            superset = names[old_zp] < names[zp] if win_new else names[zp] < names[old_zp]
            same = old_f.md5 == f.md5
            if not same:
                conflicts.append(
                    f"{f.basename}：{old_f.zip_name}({len(old_f.sentences)} 句) 与 "
                    f"{f.zip_name}({len(f.sentences)} 句) **内容不同**，"
                    f"取 {winner.zip_name}（{'超集 zip' if superset else '按启发式：条目多/时间晚'}）")
            elif superset:
                conflicts.append(f"{f.basename}：内容相同，取超集 zip {winner.zip_name}")
            else:
                conflicts.append(
                    f"{f.basename}：内容相同但 zip 无包含关系，"
                    f"按启发式取 {winner.zip_name}（请人工确认）")
            kept[f.basename] = winner
            del loser
    files = sorted(kept.values(), key=lambda f: f.basename)
    if conflicts:
        all_skipped.append(
            f"跨 zip 重名 {len(conflicts)} 个（已去重，详见 audit 的重名冲突一节）")
    return files, all_skipped


def zip_overlaps(corpus_dir: Path) -> list[tuple[str, str, int, str]]:
    """给 audit 用：两两 zip 的 basename 重叠与包含关系。"""
    zips = sorted(corpus_dir.glob("*.zip"))
    names = {zp.name: zip_basenames(zp) for zp in zips}
    out = []
    keys = sorted(names)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ov = names[a] & names[b]
            if not ov:
                continue
            if names[a] < names[b]:
                rel = f"{a} ⊂ {b}"
            elif names[b] < names[a]:
                rel = f"{b} ⊂ {a}"
            else:
                rel = "部分重叠（无包含关系）"
            out.append((a, b, len(ov), rel))
    return out


def dedup_by_md5(files: list[CorpusFile]) -> dict[str, list[CorpusFile]]:
    """按内容 md5 分组：同组只需调一次 API，但每个成员照样出 xlsx（§2 行 14）。"""
    groups: dict[str, list[CorpusFile]] = {}
    for f in files:
        groups.setdefault(f.md5, []).append(f)
    return groups
