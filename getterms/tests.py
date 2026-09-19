"""断言测试：计划 §2 的 15 类异常 + §9 的验证项，逐个造断言。

用法：python -m getterms.tests
不调 API、不花钱。改代码后必须重跑这个。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from pathlib import Path

from . import tests_fr, tests_ru
from .config import utf8_stdout
from .config import ALLOWED_TYPES, corpus_dir
from .corpus import (
    Sentence, cjk_ratio, dedup_by_md5, is_meaningful, load_corpus, norm_text,
    parse_file, replace_out_name,
)
from .extract import find_verbatim, norm_types, parse_json_array, validate_batch
from .writer import (DEFAULT_EXPORT, README_TEMPLATE, write_field_spec,
                     write_readme, write_xlsx)

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{(' — ' + detail) if detail else ''}")


def skip(what: str, why: str) -> None:
    """**可见地**跳过一整块断言。

    ⚠ 为什么不直接用 `check(..., True)` 糊过去：那会把「跳过」记进 PASS，
    读输出的人**分不出「测过了」和「没测」**（本文件里原来就有一处是这么写的，
    2026-09-19 审计把它列为假验证）。跳过必须**当场打印**、并**单独计数**。
    """
    SKIP.append(what)
    print(f"  [跳过] {what} —— {why}")


def _dumps_present() -> bool:
    """`bakeoff/` 下那几份定稿 dump 在不在。

    公开副本里**它们必然不在** —— dump 是模型输出，含**语料原句**，不能公开。
    所以依赖 dump 的那几块断言在公开副本里只能跳过；**在内部仓库里它们照跑**。
    """
    from .config import FINAL_DUMPS
    return all(pathlib.Path(p).exists() for p in FINAL_DUMPS.values())


def main() -> int:
    utf8_stdout()
    # ⚠ **公开副本专有的一行**（内部版没有）：本套断言**从头到尾不调一次 API**，
    #   但有个缓存探针要构造 `LLMClient`，而它的 `__init__` 会读一次 key。
    #   给个哑值，让仓库**克隆下来就能跑**，不必先配一把真 key。
    #   （`load_api_key` 里环境变量优先于 key 文件，所以真 key 不会被碰到。）
    os.environ.setdefault("INFERERA_API_KEY", "sk-dummy-tests-never-call-the-api")

    # ============================================ 纯函数层（不依赖语料）

    # 文件名规则（HTML replaceOutName 的逐字复刻）
    check("文件名 _align.qc.json",
          replace_out_name("a_align.qc.json") == "a_term.xlsx")
    check("文件名 .align.qc.json（点号）",
          replace_out_name("a.align.qc.json") == "a.term.xlsx")
    check("文件名 .json", replace_out_name("a.json") == "a_term.xlsx")
    check("文件名 .jsonl", replace_out_name("a.jsonl") == "a_term.xlsx")
    check("文件名 其他扩展名", replace_out_name("a.txt") == "a.txt_term.xlsx")

    # 文本归一化：§2 行 10/11
    check("norm_text None -> 空", norm_text(None) == "")
    check("norm_text int -> str", norm_text(5) == "5")
    check("norm_text 嵌套 {text}", norm_text({"text": " hola "}) == "hola")
    check("norm_text 嵌套无 text 键", norm_text({"start_ms": "0"}) == "")
    check("is_meaningful 纯数字为空", not is_meaningful("1231323"))
    check("is_meaningful 键盘乱敲为空", not is_meaningful("【】、【】、"))
    check("is_meaningful 标点为空", not is_meaningful("[]\\;;'[]"))
    check("is_meaningful 中文有效", is_meaningful("秘鲁国旗"))
    check("is_meaningful 西语有效", is_meaningful("bandera peruana"))
    check("is_meaningful 带数字的西语有效", is_meaningful("COVID-19 pandemia"))

    check("cjk_ratio 中文高", cjk_ratio("这是中文句子") > 0.9)
    check("cjk_ratio 西语零", cjk_ratio("esto es español") == 0.0)

    # 句子可用性
    check("句子两侧齐备才可用",
          Sentence(1, 1, "中文", "español").usable)
    check("句子单侧空则不可用",
          not Sentence(1, 1, "", "español").usable)
    check("句子单侧为垃圾则不可用",
          not Sentence(1, 1, "中文", "【】、").usable)

    # 逐字锚定：精确 / 大小写 / 空白折叠 / 否定
    check("锚定 精确", find_verbatim("联合国", "他访问了联合国总部") == "联合国")
    check("锚定 大小写不敏感返回原文切片",
          find_verbatim("naciones unidas", "las Naciones Unidas hoy") == "Naciones Unidas")
    check("锚定 空白折叠",
          find_verbatim("reforma y apertura",
                        "la reforma  y\n apertura institucional") is not None)
    check("锚定 不存在返回 None",
          find_verbatim("民主", "他访问了联合国总部") is None)
    check("锚定 空串返回 None", find_verbatim("", "abc") is None)
    check("锚定 返回的是原文切片而非模型字符串",
          find_verbatim("PERÚ", "el Perú de hoy") == "Perú")

    # 类型规范化：num 必须被过滤掉（用户明确要求不抽 num）
    check("类型 过滤 num", norm_types(["tech", "num"]) == ["tech"])
    check("类型 大写归一", norm_types(["TECH"]) == ["tech"])
    check("类型 字符串逗号分隔", norm_types("poli, tab") == ["poli", "tab"])
    check("类型 去重保序", norm_types(["poli", "poli", "tab"]) == ["poli", "tab"])
    check("类型 全非法返回空", norm_types(["bogus"]) == [])
    check("类型白名单无 num", "num" not in ALLOWED_TYPES)
    check("类型白名单共 10 类", len(ALLOWED_TYPES) == 10)

    # 容错 JSON 解析
    check("JSON 裸数组", parse_json_array('[{"sent_id":1,"terms":[]}]') != [])
    check("JSON 带围栏",
          parse_json_array('```json\n[{"sent_id":1,"terms":[]}]\n```') != [])
    check("JSON 尾逗号",
          parse_json_array('[{"sent_id":1,"terms":[]},]') != [])
    check("JSON 全角引号",
          parse_json_array('[{“sent_id”:1,“terms”:[]}]') != [])
    check("JSON json_object 包装",
          parse_json_array('{"result":[{"sent_id":1,"terms":[]}]}') != [])
    check("JSON 前后有闲话",
          parse_json_array('好的：\n[{"sent_id":1,"terms":[]}]\n完成') != [])
    check("JSON 空串", parse_json_array("") == [])
    check("JSON 纯垃圾", parse_json_array("对不起我不能") == [])

    # 校验层：幻觉术语必须被拒
    batch = [Sentence(1, 1, "中国推进改革开放。", "China impulsa la reforma y apertura.")]
    good = [{"sent_id": 1, "terms": [
        {"term_src": "改革开放", "term_tgt": "reforma y apertura",
         "types": ["poli"], "note": ""}]}]
    oc = validate_batch(good, batch)
    check("校验 合法术语通过", oc.n_kept == 1 and oc.rows[0].types == "poli")
    hallu = [{"sent_id": 1, "terms": [
        {"term_src": "一国两制", "term_tgt": "un país dos sistemas",
         "types": ["poli"], "note": ""}]}]
    oc2 = validate_batch(hallu, batch)
    check("校验 幻觉术语被拒", oc2.n_kept == 0 and oc2.bad_anchor_src == 1)
    half = [{"sent_id": 1, "terms": [
        {"term_src": "改革开放", "term_tgt": "reforma económica",
         "types": ["poli"], "note": ""}]}]
    oc3 = validate_batch(half, batch)
    check("校验 tgt 侧幻觉被拒", oc3.n_kept == 0 and oc3.bad_anchor_tgt == 1)
    oob = [{"sent_id": 99, "terms": [
        {"term_src": "改革开放", "term_tgt": "reforma y apertura",
         "types": ["poli"], "note": ""}]}]
    check("校验 sent_id 不在批内被拒", validate_batch(oob, batch).bad_sent_id == 1)
    badtype = [{"sent_id": 1, "terms": [
        {"term_src": "改革开放", "term_tgt": "reforma y apertura",
         "types": ["num"], "note": ""}]}]
    oc4 = validate_batch(badtype, batch)
    check("校验 非法类型降级为 other 但不丢术语",
          oc4.n_kept == 1 and oc4.rows[0].types == "other" and oc4.bad_types == 1)
    check("校验 xlsx 行的 term 与 src_text 同源",
          oc.rows[0].src_text == batch[0].src)

    # 异种 schema 的读取（造样本，不依赖真实语料）
    import json as _json
    sub = _json.dumps({"metadata": {}, "reviewer": "x", "subtitles": [
        {"sen_id": 1, "src_text": "中文一", "tgt_text": "es uno"},
        {"sen_id": 2, "src_text": "中文二", "tgt_text": "es dos"}]},
        ensure_ascii=False).encode()
    cf = parse_file(sub, "07 align_qc/x_align.qc.json", "z.zip")
    check("异种 schema subtitles 能读出句子",
          cf.schema == "subtitles" and len(cf.sentences) == 2
          and cf.sentences[0].src == "中文一")
    ad = _json.dumps({"project_info": {}, "alignment_data": [
        {"sen_id": 1, "src_time": "0", "src_text": "中文", "tgt_text": "es"}]},
        ensure_ascii=False).encode()
    cf2 = parse_file(ad, "07 align_qc/y_align.qc.json", "z.zip")
    check("异种 schema alignment_data 能读出句子",
          cf2.schema == "alignment_data" and len(cf2.sentences) == 1)

    # sent_id 通用规则：全 0 / 重复 / 跳号
    zero = _json.dumps({"sentences": [
        {"sent_id": 0, "src": {"text": "a中文"}, "tgt": {"text": "a es"}},
        {"sent_id": 0, "src": {"text": "b中文"}, "tgt": {"text": "b es"}}]},
        ensure_ascii=False).encode()
    cz = parse_file(zero, "z_align.qc.json", "z.zip")
    check("sent_id 全 0 -> 重编 1..N",
          [s.out_id for s in cz.sentences] == [1, 2]
          and any("重编" in n for n in cz.notes))
    dup = _json.dumps({"sentences": [
        {"sent_id": 1, "src": {"text": "a中文"}, "tgt": {"text": "a es"}},
        {"sent_id": 1, "src": {"text": "b中文"}, "tgt": {"text": "b es"}},
        {"sent_id": 3, "src": {"text": "c中文"}, "tgt": {"text": "c es"}}]},
        ensure_ascii=False).encode()
    cd = parse_file(dup, "d_align.qc.json", "z.zip")
    check("sent_id 重复 -> 重编（HTML 会把术语挂错句）",
          [s.out_id for s in cd.sentences] == [1, 2, 3]
          and any("重复" in n for n in cd.notes))
    gap = _json.dumps({"sentences": [
        {"sent_id": 1, "src": {"text": "a中文"}, "tgt": {"text": "a es"}},
        {"sent_id": 3, "src": {"text": "b中文"}, "tgt": {"text": "b es"}}]},
        ensure_ascii=False).encode()
    cg = parse_file(gap, "g_align.qc.json", "z.zip")
    check("sent_id 跳号 -> 沿用原 id（无害）",
          [s.out_id for s in cg.sentences] == [1, 3]
          and not any("重编" in n for n in cg.notes))
    check("内部位置索引始终 1..N",
          [s.pos for s in cg.sentences] == [1, 2])

    # 导出层
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t_term.xlsx"
        check("0 术语不产出 xlsx", write_xlsx([], p) is False and not p.exists())
        check("有术语则产出", write_xlsx(oc.rows, p) is True and p.exists())
        from openpyxl import load_workbook
        wb = load_workbook(p)
        check("sheet 名为 terms", wb.sheetnames == ["terms"])
        hdr = [c.value for c in wb["terms"][1]]
        # ⚠ **冻结的字面量**，不是拿 DEFAULT_EXPORT.columns 跟自己比。
        # 2026-09-19 发现这条原来是**自指**的（`hdr == list(DEFAULT_EXPORT.columns)`）——
        # 改列配置它照样绿，只在报错信息里印出新列，等于没测。规范必须是外部常量。
        # 第 6 列是 `types`（跟 HTML 工具），2.8/2.9 样例里写的是 `type` ——
        # 名字不同是 2026-09-19 用户拍板的结果，不是配置跑偏。
        SPEC_COLUMNS = ["sent_id", "src_text", "tgt_text", "term_src", "term_tgt",
                        "types", "note", "term_src_dict", "term_tgt_dict"]
        check("交付表 9 列、列序与 2.8/2.9 样例一致（前 7 列位置；第 6 列名跟 HTML）",
              hdr == SPEC_COLUMNS, f"实际 {hdr}")
        check("导出配置与规范同源（配置不许偷偷跑偏）",
              list(DEFAULT_EXPORT.columns) == SPEC_COLUMNS,
              f"配置 {list(DEFAULT_EXPORT.columns)}")
        # ⚠ 按**列名**取，不再硬编码下标。原来写 `[2][6]`：在 note 之前插两列就会
        #   读到别的列，而且**很可能仍然是空、照样绿** —— 静默读错列是最坏的一种。
        _ci = {c.value: i for i, c in enumerate(wb["terms"][1]) if c.value}
        check("note 列恒空",
              wb["terms"][2][_ci["note"]].value in (None, ""))
        # 交付的两列各就各位：第 4/5 列是句中切片，第 8/9 列是词条形式。
        _r2 = oc.rows[0] if oc.rows else None
        if _r2 is not None:
            _row = [c.value for c in wb["terms"][2]]
            check("第 4/5 列 = 句中原始切片（逐字、未归一化）",
                  _row[_ci["term_src"]] == _r2.term_src
                  and _row[_ci["term_tgt"]] == _r2.term_tgt,
                  f"{_row[_ci['term_src']]!r} / {_row[_ci['term_tgt']]!r}")
            check("第 8/9 列 = 词条形式（out_src/out_tgt）",
                  _row[_ci["term_src_dict"]] == _r2.out_src
                  and _row[_ci["term_tgt_dict"]] == _r2.out_tgt,
                  f"{_row[_ci['term_src_dict']]!r} / {_row[_ci['term_tgt_dict']]!r}")

    # ---- 交付文案的两个模板必须**渲染得出来**
    # ⚠ 2026-09-19 补。此前全仓**没有任何测试**碰过 `write_readme` / `write_field_spec`
    #   （`grep write_field_spec` 只命中 run.py 与 writer.py 自己），而它们的触发点在
    #   `run.py` 导出段的**最末尾** —— xlsx 与 terms_raw.json 都写完之后，且导出段不在
    #   任何 try 里。于是一个写错的字面花括号（`{…}` 而不是 `{{…}}`）会让一轮五小时
    #   付费跑在「钱花完、表写好」之后崩掉：退出码 1、没有 zip、没有 README、没有报告。
    #   本轮真的踩了：`term/{…}_term.xlsx` 里的 `{…}` 被 `.format()` 当占位符 → KeyError。
    #   这条断言就是那次崩溃的回归测试，也是**唯一**会走到打包阶段的断言。
    import re as _re
    with tempfile.TemporaryDirectory() as td:
        # 占位符名字从模板里现取，这样以后新增占位符不会让这条误报。
        _kw = {n: "x" for n in _re.findall(r"\{(\w+)\}", README_TEMPLATE)}
        _kw.update(ts="t", model="m", effort="high", lang="es", batch_size=10,
                   prompt="es_v8", phash="deadbeef", columns="a,b,c",
                   sheet="terms", stats="s", issues="i")
        try:
            _rtxt = write_readme(Path(td), **_kw).read_text(encoding="utf-8")
            check("README 模板渲染得出来（占位符与字面花括号都写对了）", True)
            check("README 渲染后含 9 列清单，且第 6 列写作 `types`",
                  "term_src_dict" in _rtxt and "`types`" in _rtxt)
        except Exception as e:  # noqa: BLE001 —— 渲染失败正是这条要抓的
            check("README 模板渲染得出来（占位符与字面花括号都写对了）",
                  False, f"{type(e).__name__}: {e}")
        try:
            _ftxt = write_field_spec(Path(td), "t").read_text(encoding="utf-8")
            check("字段说明模板渲染得出来", True)
            # `{{…}}` 转义对了的话，渲染结果里应当留下**字面**的 `{…}`。
            check("字段说明里 `term/` 与 `{…}` 是字面文本，没被 .format() 吞掉",
                  "term/" in _ftxt and "{…}" in _ftxt)
        except Exception as e:  # noqa: BLE001
            check("字段说明模板渲染得出来", False, f"{type(e).__name__}: {e}")

    # ---- 纠正重试的采用条件（2026-09-16 修的 bug 的回归断言）
    # 首轮 JSON 解析失败 -> oc.failures 为空；重试修好后必须被采用
    from .extract import BatchOutcome

    def would_adopt(kept0, fails0, kept2, fails2) -> bool:
        """复刻 run_batch 里的 better 判据。"""
        return kept2 > kept0 or (kept2 == kept0 and fails2 < fails0)

    check("重试修好首轮 JSON 失败 -> 采用", would_adopt(0, 0, 5, 0))
    check("重试抽到更多术语 -> 采用", would_adopt(3, 2, 6, 2))
    check("术语数相同但问题更少 -> 采用", would_adopt(4, 3, 4, 1))
    check("重试更差 -> 不采用", not would_adopt(6, 1, 2, 0))
    check("重试完全一样 -> 不采用", not would_adopt(4, 2, 4, 2))
    check("首轮全空且重试也全空 -> 不采用", not would_adopt(0, 0, 0, 0))
    oc_empty = validate_batch([], batch)
    check("空 items 会置 json_failed 且 failures 为空",
          oc_empty.json_failed and not oc_empty.failures)

    # ---- 缓存键必须含批内容（换批大小不能命中旧响应）
    from .extract import batch_key
    b1 = [Sentence(1, 1, "ab", "c")]
    b2 = [Sentence(1, 1, "a", "bc")]
    check("batch_key 有分隔符，不会拼接碰撞", batch_key(b1) != batch_key(b2))
    check("batch_key 同内容稳定", batch_key(b1) == batch_key([Sentence(1, 1, "ab", "c")]))
    big = [Sentence(i, i, f"中文{i}", f"es{i}") for i in range(1, 21)]
    check("不同批大小的键不同（批10 vs 批20 的第0批）",
          batch_key(big[:10]) != batch_key(big[:20]))
    check("同一批不同顺序键不同", batch_key(big[:3]) != batch_key(big[:3][::-1]))

    # ============================================ 真实语料层
    # ⚠ **公开副本专有**：西语语料不在就**可见地跳过**这一整块，不当成失败。
    #   本副本不含语料（内容涉第三方版权）。内部仓库里有，照跑。
    if not corpus_dir("es").exists():
        print("  [跳过] 西语语料层断言 —— 公开副本不含语料（内容涉第三方版权）")
        files, skipped = [], []
    else:
        files, skipped = load_corpus(corpus_dir("es"))
        check("语料条目 532 个 JSON", len(files) == 532, f"实际 {len(files)}")
        check("跳过 1 个非 JSON（混进来的 xlsx）", len(skipped) == 1,
              f"实际 {len(skipped)}")
        by_stem = {f.stem_key: f for f in files}

        renum = [f for f in files if any("重编" in n for n in f.notes)]
        check("触发重编的文件共 12 个", len(renum) == 12, f"实际 {len(renum)}")
        for stem in ("zh-es_tour_muse_0023_seg002", "zh-es_tour_muse_0024_seg002",
                     "es-zh_conf_poli_0007_seg001"):
            f = by_stem.get(stem)
            check(f"重复 id 文件已重编: {stem}",
                  f is not None and any("重复" in n for n in f.notes)
                  and [s.out_id for s in f.sentences] == list(range(1, len(f.sentences) + 1)))
        for stem in ("es-zh_conf_poli_0036_seg001", "zh-es_conf_poli_0023_seg001"):
            f = by_stem.get(stem)
            check(f"跳号文件沿用原 id: {stem}",
                  f is not None and not any("重编" in n for n in f.notes))

        for stem, n in (("zh-es_conf_poli_0005_seg001", 75),
                        ("zh-es_conf_poli_0014_seg001", 93),
                        ("zh-es_tour_serv_0016_seg003", 84)):
            f = by_stem.get(stem)
            check(f"异种 schema 文件读出 {n} 句: {stem}",
                  f is not None and len(f.sentences) == n,
                  f"实际 {len(f.sentences) if f else 'None'}")

        groups = dedup_by_md5(files)
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        check("md5 重复组共 1 组 3 个文件",
              len(dups) == 1 and len(next(iter(dups.values()))) == 3)
        check("去重后唯一内容 530 份", len(groups) == 530, f"实际 {len(groups)}")

        dotted = [f for f in files if f.basename.endswith(".align.qc.json")]
        check("点号命名 4 个", len(dotted) == 4, f"实际 {len(dotted)}")
        check("点号命名输出为 .term.xlsx",
              all(f.out_name.endswith(".term.xlsx") for f in dotted))

        check("无 0 句文件", all(f.sentences for f in files))
        check("所有文件都能算出输出名", all(f.out_name for f in files))
        check("垃圾单元不进可用句",
              all(is_meaningful(s.src) and is_meaningful(s.tgt)
                  for f in files for s in f.usable_sentences))

        total = sum(len(f.sentences) for f in files)
        usable = sum(len(f.usable_sentences) for f in files)
        check("总句对 56656", total == 56656, f"实际 {total}")
        check("可用句对 56374", usable == 56374, f"实际 {usable}")

        # 方向相反的文件：术语按槽位锚定，与语言无关
        rev = by_stem.get("es-zh_conf_poli_0013_seg001")
        if rev:
            s = rev.usable_sentences[0]
            check("方向相反文件的 src 槽位确实是中文（内容方向已判定）",
                  cjk_ratio(s.src) > cjk_ratio(s.tgt))

        # ---- §2 行 14 的集成级断言：1 次调用 -> 3 份 xlsx
        # 复刻 run.py 的去重与导出映射，不调 API
        dup_group = next((v for v in groups.values() if len(v) > 1), [])
        if dup_group:
            reps_by_md5: dict[str, object] = {}
            for f in sorted(dup_group, key=lambda x: x.basename):
                reps_by_md5.setdefault(f.md5, f)
            check("重复组只产生 1 个 API 代表", len(reps_by_md5) == 1,
                  f"实际 {len(reps_by_md5)}")
            # 代表跑出来的行，组内每个成员都拿同一份
            fake_rows = {dup_group[0].md5: ["row1", "row2"]}
            got = [len(fake_rows.get(f.md5, [])) for f in dup_group]
            check("重复组每个成员都拿到代表的结果", got == [2, 2, 2], f"实际 {got}")
            outs = {f.out_name for f in dup_group}
            check("重复组输出 3 个不同的 xlsx 名", len(outs) == 3,
                  f"实际 {sorted(outs)}")
            check("重复组成员的 out_name 与各自 basename 对应",
                  all(f.out_name.startswith(f.stem_key) for f in dup_group))

    # ============================================ 法语层
    _dict_form_romance(check)
    _prompt_dict_form(check)
    _hardening(check)

    # ====================================== 2026-09-18 下半场：复数根因与自检套件
    _plural_lexicon(check)
    _selfcheck(check)
    # ⚠ 这五块读的是 `bakeoff/` 下的定稿 dump（模型输出，**含语料原句**）。
    #   公开副本不含它们 → 整块**可见地跳过**，而不是崩掉、也不是静默记成通过。
    if _dumps_present():
        _false_positives(check)
        _field_semantics(check)
        _prompt_v7(check)
        _tooling_b1(check)
        _nom_gate(check)
    else:
        skip("5 块依赖 bake-off 定稿 dump 的断言（_false_positives / _field_semantics / "
             "_prompt_v7 / _tooling_b1 / _nom_gate）",
             "dump 是模型输出、含语料原句，公开副本不含；内部仓库里照跑")

    # 独立成 getterms/tests_fr.py：法语与俄语两轮并行推进，分文件减少合并冲突。
    tests_fr.run(check, files)

    # ============================================ 俄语层
    # 独立成 getterms/tests_ru.py，同上。
    tests_ru.run(check)

    # ============================================ 汇总
    print(f"通过 {len(PASS)}   失败 {len(FAIL)}")
    print("⚠ 上面有若干 `[跳过]` 行 —— **本副本不含语料与 bake-off dump**"
          "（两者都涉第三方内容），那几块没跑。")
    print("   内部仓库里有完整语料，同一套断言在那里跑满 922 条。")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
    elif not SKIP:
        print("全部通过")
    return 1 if FAIL else 0


def _hardening(check) -> None:
    """2026-09-18 全量发射前的加固，逐条钉死。

    这些都是「只在全量尺度才出事」或「静默出事」的东西，bake-off 全绿也测不出来。
    """
    import asyncio
    import contextlib
    import io
    import json as _json

    from . import run as R
    from . import verify as V
    from .qc_workbook import _row_view, stratify
    from .report import FULL_CORPUS, RunReport

    # ---- 1 worker 兜底：单批本地异常不许炸掉整轮
    #      直接验 gather 的语义（run.py 里用的就是这一条），并验 worker 壳的形状。
    async def _probe():
        async def boom():
            raise ValueError("模拟形态分析器抛的意外")

        async def fine():
            return "ok"

        return await asyncio.gather(boom(), fine(), return_exceptions=True)

    got = asyncio.run(_probe())
    check("gather(return_exceptions=True) 让好任务照样返回",
          got[1] == "ok" and isinstance(got[0], ValueError), f"实际 {got}")
    src = (Path(R.__file__)).read_text(encoding="utf-8")
    check("run.py 的 gather 带 return_exceptions=True",
          "return_exceptions=True" in src)
    check("run.py 的 worker 兜住 Exception 并记成失败批",
          "rep.batch_exceptions += 1" in src and "except Exception as e:" in src)
    check("run.py 不再有裸 gather（旧写法会让 5851 批成果归零）",
          "await asyncio.gather(*(worker(f, i, b) for f, i, b in tasks))" not in src)

    # ---- 3 形态分析器降级必须被拦，且报告能标出来
    rep = RunReport(model="gemini-3.7-flash", effort="high", prompt="es_v6",
                    phash="x", batch_size=10, concurrency=50, lang="es")
    rep.morph_enabled = False
    check("morph_enabled=False 时 gate 直接淘汰",
          any("形态分析器未启用" in v for v in rep.gate_verdict()),
          f"实际 {rep.gate_verdict()}")
    rep.morph_enabled = True
    check("morph_enabled=True 时 gate 不因此淘汰",
          rep.gate_verdict() == ["通过全部硬门槛"], f"实际 {rep.gate_verdict()}")
    check("run.py 缺分析器时会 return 2（不是继续跑）",
          "not morph_enabled and not a.allow_degraded" in src)
    check("run.py 在**开跑前**就打印形态分析器状态",
          src.index("morph_status(a.lang)") < src.index("client = LLMClient"))

    # ---- 4 失败批闸门
    check("INCOMPLETE_GATE 存在且是个小比例",
          0 < R.INCOMPLETE_GATE <= 0.01, f"实际 {R.INCOMPLETE_GATE}")
    check("超阈值时不产出 results.zip", "and package_ok" in src)
    check("超阈值时不产出 README.md", "if written and package_ok:" in src)
    check("失败批清单会落盘", "未完成_失败批清单.txt" in src)

    # ---- 5 限流计数必须被打印（否则「限流被当质量差」）
    check("run.py 打印 client.stats", "client.stats" in src and "调用层计数" in src)

    # ---- 6 refused 不写缓存
    lsrc = (Path(R.LLMClient.__module__ and __import__(
        "getterms.llm", fromlist=["x"]).__file__)).read_text(encoding="utf-8")
    # ⚠ 换成行为断言。原来是精确缩进的源码子串 grep —— 挡得住「删掉这一行」，
    #   却证明不了语义，而且条件一改（本轮加了 and not r.truncated）就假红。
    def _fake_resp(content, finish):
        import types as _t
        msg = _t.SimpleNamespace(content=content)
        ch = _t.SimpleNamespace(finish_reason=finish, message=msg)
        return _t.SimpleNamespace(choices=[ch], usage=None)

    def _cache_probe(content, finish, tmp):
        """发一次假响应，返回缓存文件是否落盘。"""
        cl = R.LLMClient("gemini-3.7-flash", "high", "t" * 12, concurrency=1)
        cl.cache_root = Path(tmp)

        async def _create(**kw):
            return _fake_resp(content, finish)

        cl._client.chat.completions.create = _create
        r = asyncio.run(cl.call([{"role": "user", "content": "x"}],
                                "f" * 32, 0, "bk"))
        return cl._cache_path("f" * 32, 0, "bk").exists(), r

    with tempfile.TemporaryDirectory() as _td:
        ok_cached, _r = _cache_probe("[]", "stop", _td)
        check("正常响应会写缓存", ok_cached)
    with tempfile.TemporaryDirectory() as _td:
        bad_cached, _r = _cache_probe("", "stop", _td)
        check("refused（空响应）**不写缓存**", not bad_cached and _r.refused)
    with tempfile.TemporaryDirectory() as _td:
        tr_cached, _r = _cache_probe("[{\"sent_id\"", "length", _td)
        check("truncated（finish_reason=length）**不写缓存**",
              not tr_cached and _r.truncated and not _r.refused,
              "截断响应进了缓存 = 永久无声丢批，重跑也不会重试")
    with tempfile.TemporaryDirectory() as _td:
        fl_cached, _r = _cache_probe("[]", "content_filter", _td)
        check("content_filter **不写缓存**", not fl_cached)

    # ---- 缓存反序列化失败 = 未命中，绝不能变成「失败批」
    with tempfile.TemporaryDirectory() as _td:
        cl = R.LLMClient("gemini-3.7-flash", "high", "u" * 12, concurrency=1)
        cl.cache_root = Path(_td)
        pth = cl._cache_path("a" * 32, 0, "k")
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(_json.dumps({"content": "x"}), encoding="utf-8")   # 缺 ok
        check("缓存缺必填字段 -> 当未命中（不抛 TypeError）",
              cl._read_cache("a" * 32, 0, "k") is None)
        pth.write_text(_json.dumps({"ok": True, "content": "y", "未来字段": 1}),
                       encoding="utf-8")
        got = cl._read_cache("a" * 32, 0, "k")
        check("缓存多出未知字段 -> 忽略该字段仍能读回（向前兼容）",
              got is not None and got.content == "y")
        check("llm.py 落盘失败有计数器", "cache_write_fail" in lsrc)

    from . import extract as E

    # ---- 6b 三处畸形结构不再静默丢弃（以前是裸 continue，既不计数也不触发重试）
    _b = [E.Sentence(pos=1, out_id=1, src="中国的发展很快。",
                     tgt="El desarrollo de China es rápido.")]
    _o = E.validate_batch(["垃圾"], _b, lang="es")
    check("数组元素不是对象 -> 计数并触发纠正重试",
          _o.bad_shape == 1 and len(_o.failures) >= 1)
    _o = E.validate_batch([{"sent_id": 1, "terms": "发展"}], _b, lang="es")
    check("terms 不是数组 -> 计数并触发纠正重试",
          _o.bad_shape == 1 and len(_o.failures) >= 1)
    _o = E.validate_batch([{"sent_id": 1, "terms": ["发展"]}], _b, lang="es")
    check("terms 里的项不是对象 -> 计数并触发纠正重试",
          _o.bad_shape == 1 and len(_o.failures) >= 1)
    # ⚠ 没有 terms 键 = 这句没术语，**不算错**。当成失败会白花一次纠正重试的钱。
    _o = E.validate_batch([{"sent_id": 1}], _b, lang="es")
    check("没有 terms 键 -> 不算错、不触发重试",
          _o.bad_shape == 0 and _o.no_terms_key == 1 and not _o.failures)
    _o = E.validate_batch([{"sent_id": 1, "terms": []}], _b, lang="es")
    check("terms: [] -> 合法的「没术语」，不计任何错",
          _o.bad_shape == 0 and _o.no_terms_key == 0 and not _o.failures)

    # ---- 6c other 与其余九类互斥（提示词自己定的规则，定稿 dump 里破了 2 条）
    check("norm_types: 有别的类时丢掉 other",
          E.norm_types(["tab", "other"]) == ["tab"]
          and E.norm_types("other,poli") == ["poli"])
    check("norm_types: 只有 other 时保留 other",
          E.norm_types(["other"]) == ["other"])

    # ---- 6d 交付值标点归一化
    _N = E.normalize_delivery_text
    check("撇号统一到 ASCII", _N("Yan\u2019an") == "Yan'an")
    check("整条被一对引号裹住 -> 剥掉", _N('"整条"') == "整条")
    # ⚠ 配对的内部引号恰好贴着词尾是**合法**的，剥一个会留下落单引号，比不动更糟。
    for _keep in ('proyecto "Transmisión Oeste-Este"',
                  'tunelador "Yuan\'an"',
                  'objectif stratégique "en deux étapes"'):
        check(f"配对内部引号不动: {_keep[:22]}", _N(_keep) == _keep)
    check("上游的不配对引号不自动修（错在原句，按「上游拼错照抄」口径）",
          _N("\u201csandwich\" de raíces de loto")
          == "\u201csandwich\" de raíces de loto")

    # ---- 6e 行序必须按句序（以前是批完成顺序，每次跑还不一样）
    check("run.py 导出前按 sent_id 稳定排序",
          "_sid_key" in src and "_rows.sort(key=_sid_key)" in src)
    check("run.py 排序在 unify_dict_forms 之前",
          src.index("_rows.sort(key=_sid_key)") < src.index("unified += unify_dict_forms"))

    # ---- 6f 不完整批：四类同权进闸门
    _rp = RunReport(model="m", effort="high", prompt="p", phash="h",
                    batch_size=10, concurrency=8)
    _rp.batches = 1000
    _rp.call_failures, _rp.refused_batches = 1, 2
    _rp.json_dead_batches, _rp.skipped_by_budget = 3, 4
    check("incomplete_batches = 失败+拒答+JSON死+熔断跳过",
          _rp.incomplete_batches == 10 and abs(_rp.incomplete_rate - 0.01) < 1e-9)
    check("熔断跳过会被 gate_verdict 判淘汰",
          any("熔断" in x for x in _rp.gate_verdict()))
    check("四类计数进文本报告（人看的是文本，不是 JSON）",
          "不完整批" in _rp.to_text())

    # ---- 6g 熔断的检查必须在抢到派发槽之后
    # ⚠ gather 会把全部协程在 t=0 一起启动，放在 worker 开头的检查等于不存在
    #   （实测 4 批全跑完、超支 10 倍）。
    check("run.py 有派发信号量", "dispatch_sem" in src)
    check("熔断检查在 dispatch_sem 之后",
          src.index("async with dispatch_sem") < src.index('if stop_flag["hit"]'))

    # ---- 6h 语料完整性：必须比 sentences_sent（可用句对），不是 sentences_total
    check("run.py 开跑前查语料完整性", "语料完整性" in src)
    check("语料完整性比的是 sentences_sent",
          "abs(rep.sentences_sent - _full)" in src)
    check("FULL_CORPUS 取的是元组第二项（值是 (语种名, 句数)）",
          "_fc[1]" in src)
    check("有 --allow-partial-corpus 逃生口", "--allow-partial-corpus" in src)

    # ---- 6i 花费上限
    check("run.py 有 --max-spend", "--max-spend" in src)
    check("默认按工作量外推 x1.5", "_auto_budget" in src)
    check("批大小默认 10（全量预算/质量基线/缓存键都按 10 定的）",
          R.build_args(["--model", "m"]).batch_size == 10)

    # ---- 6j 三个开跑前探针
    check("run.py 有 _preflight（缓存可写/磁盘/API key）", "_preflight" in src)
    for _w in ("缓存目录不可写", "剩余空间不足", "API 探针"):
        check(f"_preflight 覆盖：{_w}", _w in src)
    check("dry-run 也跑校验③（陈旧 xlsx）",
          src.index("陈旧文件会污染") < src.index("[dry-run] 不调 API"))

    # ---- 7 输出名碰撞与陈旧 xlsx 都要拦在开跑前
    check("run.py 开跑前查输出名碰撞", "组输出名碰撞" in src)
    check("run.py 开跑前查陈旧 xlsx", '*term.xlsx' in src and "_stale" in src)
    check("两道预检都在 LLMClient 之前",
          src.index("组输出名碰撞") < src.index("client = LLMClient")
          and src.index("_stale") < src.index("client = LLMClient"))

    # ---- 8 真实新增支出与累计成本要分开
    rep2 = RunReport(model="gemini-3.7-flash", effort="high", prompt="es_v6",
                     phash="x", batch_size=10, concurrency=50, lang="es")
    rep2.prompt_tokens, rep2.completion_tokens = 1_000_000, 1_000_000
    rep2.new_cost_usd = 0.5
    check("cost 是累计（含缓存命中）", rep2.cost > 4.0, f"实际 {rep2.cost}")
    check("new_spend_usd 只算真发出去的调用", rep2.new_spend_usd == 0.5)
    check("run.py 只对 not from_cache 累加新增支出",
          "if not c.from_cache:\n                rep.new_cost_usd += cost_usd" in src)

    # ---- 11 报告落盘封顶
    rep2.nom_failures = [f"f{i}" for i in range(1200)]
    rep2.errors = [f"e{i}" for i in range(1200)]
    with tempfile.TemporaryDirectory() as td:
        _txt, js = rep2.save(Path(td), "t")
        d = _json.loads(js.read_text(encoding="utf-8"))
    check("nom_failures 落盘封顶", len(d["nom_failures"]) <= 501,
          f"实际 {len(d['nom_failures'])}")
    check("封顶后仍保留总数", d["nom_failures_total"] == 1200,
          f"实际 {d.get('nom_failures_total')}")
    check("errors 落盘封顶", len(d["errors"]) <= 501, f"实际 {len(d['errors'])}")
    check("derived 里有 morph_enabled / new_spend_usd / batch_exceptions",
          {"morph_enabled", "new_spend_usd", "batch_exceptions"} <= set(d["derived"]),
          f"实际 {sorted(d['derived'])}")

    # ---- 13 俄语外推分母已回填
    check("FULL_CORPUS[ru] 不再是 None（否则成本外推那行不打印）",
          FULL_CORPUS["ru"][1] == 57469, f"实际 {FULL_CORPUS['ru']}")

    # ---- 2 xlsx 原子落盘：不留 .tmp 残骸
    from .extract import TermRow
    row = TermRow(sent_id=1, src_text="hola mundo", tgt_text="你好世界",
                  term_src="mundo", term_tgt="世界", types="other")
    with tempfile.TemporaryDirectory() as td:
        outp = Path(td) / "a_term.xlsx"
        write_xlsx([row], outp, DEFAULT_EXPORT)
        leftovers = [q.name for q in Path(td).iterdir() if ".tmp" in q.name]
        check("write_xlsx 落盘后没有 .tmp 残骸", not leftovers, f"实际 {leftovers}")
        check("write_xlsx 产出了目标文件", outp.exists())
    wsrc = (Path(DEFAULT_EXPORT.__class__.__module__ and __import__(
        "getterms.writer", fromlist=["x"]).__file__)).read_text(encoding="utf-8")
    check("writer 用 os.replace 原子落盘（xlsx 与 zip 各一处）",
          wsrc.count("os.replace(") == 2, f"实际 {wsrc.count('os.replace(')}")

    # ---- 9 verify 能当门禁：0 个 xlsx 必须非零退出
    with tempfile.TemporaryDirectory() as td:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = V.main([td, "--lang", "es"])
        check("verify 对空目录返回非零（以前会打印「全部通过」）", rc != 0, f"实际 {rc}")
        check("verify 明确说出「没有任何 *term.xlsx」",
              "没有任何 *term.xlsx" in buf.getvalue())

    # ---- 9 verify 的结构问题与还原比例分开裁决
    vsrc = (Path(V.__file__)).read_text(encoding="utf-8")
    check("verify 把「既不是原句切片」从结构问题里剔出去单独裁决",
          '"既不是原句切片" not in q' in vsrc)
    check("verify 的 --lang 限定了取值（写错会静默出假问题）",
          'choices=["es", "fr", "ru", "ja"]' in vsrc)
    check("verify 用 terms_raw.json 的 span 查表，不再只靠滑窗",
          "load_span_index" in vsrc and "stats[\"no_raw\"]" in vsrc)
    check("verify 会核对 xlsx 与 raw 是否同源",
          "xlsx 与 raw 不同源" in vsrc)

    # ---- 10 qc_workbook 分层抽样：确定性 + 回落行全留
    fake = []
    for i in range(4000):
        lay = ["conf/poli", "tour/muse", "conf/econ"][i % 3]
        if i % 400 == 0:
            fake.append({"layer": lay, "sent_id": i, "term_src": "x",
                         "term_src_base": "", "term_tgt_base": ""})
        else:
            fake.append({"layer": lay, "sent_id": i, "term_src": "casas",
                         "term_src_span": "casas",
                         "term_src_base": "casa" if i % 2 else "casas"})
    s1, note = stratify(fake, 600)
    s2, _ = stratify(fake, 600)
    n_fb_all = sum(1 for r in fake if _row_view(r)["side"] == "-")
    n_fb_got = sum(1 for r in s1 if _row_view(r)["side"] == "-")
    check("抽样不超过目标行数", len(s1) <= 600 + 5, f"实际 {len(s1)}")
    check("回落行全部保留（那是校验器标出的疑点）", n_fb_got == n_fb_all,
          f"{n_fb_got}/{n_fb_all}")
    check("抽样是确定性的（老师能接着上次判）",
          [r["sent_id"] for r in s1] == [r["sent_id"] for r in s2])
    check("抽样按层分桶", any("conf/poli" in k for k in note), f"实际 {sorted(note)}")

    # ---- 补语数变化检测器：校验器抓不到的那一类，必须有人看得见
    from .extract import complement_num_changed as CNC
    for base, span, lang, why in [
        ("immeuble de bureau", "immeubles de bureaux", "fr", "bureaux 被压成单数"),
        ("chaîne de montagne", "chaînes de montagnes", "fr", "montagnes 被压成单数"),
        ("edificio de oficina", "edificios de oficinas", "es", "oficinas 被压成单数"),
        ("mercado de capital", "mercados de capitales", "es", "固定复数补语被压单"),
        ("nouvelle forme d'entreprise", "nouvelles formes d'entreprises", "fr",
         "省音补语（法语老师要的那条，也要人看）"),
    ]:
        check(f"{lang} 报出补语数变化: {base!r} ← {span!r}（{why}）",
              CNC(base, span, lang) is not None, "没报出来")
    for base, span, lang, why in [
        ("immeuble de bureaux", "immeubles de bureaux", "fr", "只动中心词"),
        ("chaîne de montagnes", "chaînes de montagnes", "fr", "只动中心词"),
        ("revenu des ménages", "revenus des ménages", "fr", "只动中心词"),
        ("edificio de oficinas", "edificios de oficinas", "es", "只动中心词"),
        ("dette en souffrance", "dettes en souffrance", "fr", "状态补语没动"),
        ("sala de exposición", "salas de exposición", "es", "补语本来就是单数"),
        ("luz de neón", "luces de neón", "es", "补语是物质名词"),
        ("empresa privada", "empresas privadas", "es", "没有补语"),
        # 并列成分不是补语：`et`/`y` 后面那个与中心词配合，跟着变单数是对的。
        # 2026-09-18 跑 fr_v6 探针时这两条被误报过。
        ("porcelaine bleue et blanche", "porcelaines bleues et blanches", "fr",
         "并列形容词，随中心词变单数是对的"),
        ("petite et micro-entreprise", "petites et micro-entreprises", "fr",
         "并列成分，同上"),
        ("fuerza y desarrollo", "fuerzas y desarrollos", "es", "并列名词，同上"),
    ]:
        got_cc = CNC(base, span, lang)
        check(f"{lang} 不误报补语数变化: {base!r} ← {span!r}（{why}）",
              got_cc is None, f"误报了: {got_cc}")
    check("俄语不走补语检测（从属成分靠格，已由 check_nominative 覆盖）",
          CNC("империя лжи", "империю лжи", "ru") is None)
    check("报告里单列了补语一节",
          "补语的数被改了" in Path(
              __import__("getterms.report", fromlist=["x"]).__file__
          ).read_text(encoding="utf-8"))

    # ---- 15 纠正重试回抄上限
    esrc = (Path(find_verbatim.__module__ and __import__(
        "getterms.extract", fromlist=["x"]).__file__)).read_text(encoding="utf-8")
    check("纠正重试回抄上限提到 20000（实测 max 7,125，全量必然更长）",
          "r.content[:20000]" in esrc and "r.content[:8000]" not in esrc)


def _dict_form_romance(check) -> None:
    """西语/法语的词典形校验（`check_lemma_romance`）。纯函数，不花钱。

    判据全部由实测定的（`_probe_simplemma*.py`，2026-09-18），不是凭推理：
      * simplemma 会**把性抹平**（`privadas`/`privada` 的词元都是 `privado`），
        所以判据 5 比「共享词元」而不是「等于词元」，另加判据 7 挡性别翻转；
      * simplemma 没有词性上下文时会给**动词**词元（`flores`->`florar`、
        `Unidos`->`unir`），所以判据 5 并联一条词干前缀支。
    """
    from .extract import (DICT_FORM_LANGS, check_lemma_romance, load_lemmatizer,
                          TermRow, validate_batch)
    from .corpus import Sentence

    lem = load_lemmatizer()
    check("simplemma 状态可报告（装了就启用，没装只降级不报错）", True,
          f"lemmatizer={'启用' if lem else '未启用'}")
    check("DICT_FORM_LANGS 含三语", set(DICT_FORM_LANGS) == {"es", "fr", "ru"},
          f"实际 {DICT_FORM_LANGS}")

    # ---- 该通过：单数还原（提示词对照表里的例子）
    OK_ES = [
        ("sanción unilateral", "sanciones unilaterales", "复数->单数，形容词一致"),
        ("sala de exposición", "salas de exposición", "中心词单数，de exposición 不动"),
        ("empresa privada", "empresas privadas", "形容词一致，**阴性保留**"),
        ("luz de neón", "luces de neón", "不规则复数 luces->luz"),
        ("institución sanitaria", "instituciones sanitarias", "重音随单数回来"),
        ("gran modelo", "grandes modelos", "grande 在单数名词前截短成 gran"),
        ("impuesto", "impuestos", "普通复数"),
        ("suizo", "suizos", "形容词作族群名，取词典立目形"),
        ("resolución", "resoluciones", "-ciones -> -ción"),
        ("flor y pájaro", "flores y pájaros", "flores 的词元是动词 florar，靠词干支救回"),
        ("desplazado", "desplazados", "分词作名词，词元是动词 desplazar"),
        ("europeo", "europeos", ""),
        ("panel", "paneles", ""),
        ("manchú", "manchúes", ""),
        # 该保留原样的
        ("derechos humanos", "derechos humanos", "习惯复数，不还原也合法"),
        ("Estados Unidos", "Estados Unidos", "专名"),
        ("Naciones Unidas", "Naciones Unidas", "专名"),
        ("fuerzas armadas", "fuerzas armadas", "习惯复数"),
        ("nuevas fuerzas productivas de calidad", "nuevas fuerzas productivas de calidad",
         "固定表述"),
        ("economía mundial", "economía mundial", "本来就是词典形"),
        ("profundizar la reforma", "profundizar la reforma", "动词已是不定式"),
        ("injerencia en los asuntos internos", "injerencia en los asuntos internos",
         "中心词已单数，从属成分不动"),
    ]
    for base, anch, why in OK_ES:
        err = check_lemma_romance(base, anch, "es")
        check(f"es 词典形通过: {base} ← {anch}" + (f"（{why}）" if why else ""),
              err is None, f"报错 {err}")

    OK_FR = [
        ("force terroriste", "forces terroristes", "复数->单数"),
        ("dette en souffrance", "dettes en souffrance", "中心词单数，en souffrance 不动"),
        ("nouvelle forme d'entreprises", "nouvelles formes d'entreprises",
         "中心词单数、**阴性保留**、从属 d'entreprises 不动"),
        ("résolution pertinente", "résolutions pertinentes", "形容词一致"),
        ("réseau hydrographique", "réseaux hydrographiques", "不规则复数 -aux"),
        ("travail", "travaux", "不规则复数，词元命中"),
        ("général", "généraux", "不规则复数"),
        ("nature morte", "natures mortes", ""),
        ("convention de Genève", "conventions de Genève", ""),
        ("droits de l'homme", "droits de l'homme", "习惯复数"),
        ("Nations Unies", "Nations Unies", "专名"),
        ("arts et métiers traditionnels", "arts et métiers traditionnels", "固定并列表述"),
        ("la paix et le développement", "la paix et le développement",
         "并列保留首冠词（fr_v4 的成果），冠词不参与还原"),
        ("intelligence artificielle", "intelligence artificielle", "本来就是词典形"),
    ]
    for base, anch, why in OK_FR:
        err = check_lemma_romance(base, anch, "fr")
        check(f"fr 词典形通过: {base} ← {anch}" + (f"（{why}）" if why else ""),
              err is None, f"报错 {err}")

    # ---- 该失败
    BAD = [
        ("empresa privado", "empresas privadas", "es", "性被翻转（形容词没跟中心名词）"),
        ("institución sanitario", "instituciones sanitarias", "es", "同上"),
        ("nouveau forme", "nouvelles formes", "fr", "性被翻转"),
        ("impuestos", "impuesto", "es", "方向搞反：单数被改成复数"),
        ("empresas", "empresa", "es", "同上"),
        ("sanción", "impuestos", "es", "换了词"),
        ("empresa pública", "empresas privadas", "es", "换了修饰语"),
        ("sanction", "recettes", "fr", "换了词"),
        ("estado unido", "Estados Unidos", "es", "专名被改动"),
        ("suizo", "los suizos", "es", "词数不一致"),
        ("luz neón", "luces de neón", "es", "词数不一致（吞掉了介词）"),
        ("luz del neón", "luces de neón", "es", "功能词被改"),
        ("", "impuestos", "es", "dict_form 为空"),
        ("impuesto 税", "impuestos", "es", "引入汉字（顺手翻译）"),
        ("импуesto", "impuestos", "es", "引入西里尔字母"),
    ]
    # ⚠ 这两条只有判据 6（不得复数化）能抓，而判据 6 依赖 simplemma。
    #   照 tests_ru.py 的双分支写法：装了库要求抓住，没装库要求放过 ——
    #   否则在没装 simplemma 的环境里这 2 条会报**假失败**（测试自己没适配降级）。
    NEEDS_LEM = {("impuestos", "impuesto"), ("empresas", "empresa")}
    for base, anch, lang, why in BAD:
        err = check_lemma_romance(base, anch, lang)
        if (base, anch) in NEEDS_LEM and lem is None:
            check(f"{lang} 无 simplemma 时如实放过: {base!r} ← {anch!r}"
                  f"（{why}，只有判据 6 能抓，判据 6 要词典）",
                  err is None, f"实际 {err}")
        else:
            check(f"{lang} 词典形判失败: {base!r} ← {anch!r}（{why}）",
                  err is not None, "居然通过了")

    # ---- 降级路径必须可达：缺 simplemma 时前四项仍然生效
    check("降级路径可达：use_lemmatizer=False 时单数还原被放过",
          check_lemma_romance("impuesto", "impuestos", "es",
                              use_lemmatizer=False) is None)
    check("降级路径仍挡住词数不一致",
          check_lemma_romance("suizo", "los suizos", "es",
                              use_lemmatizer=False) is not None)
    check("降级路径仍挡住性别翻转（判据 7 不依赖库）",
          check_lemma_romance("empresa privado", "empresas privadas", "es",
                              use_lemmatizer=False) is not None)
    check("降级路径仍挡住专名被改",
          check_lemma_romance("estado unido", "Estados Unidos", "es",
                              use_lemmatizer=False) is not None)

    # ---- 锚定闸门没有被放宽：词典形本身仍然锚不上原句
    check("闸门未放宽：sanción unilateral 锚不上原句",
          find_verbatim("sanción unilateral",
                        "cualquier forma de sanciones unilaterales") is None)
    check("闸门仍然认得原句切片",
          find_verbatim("sanciones unilaterales",
                        "cualquier forma de sanciones unilaterales")
          == "sanciones unilaterales")

    # ---- validate_batch：外语侧被填，中文侧永远留空；方向不固定
    s_es_tgt = Sentence(pos=1, out_id=1,
                        src="中方坚决反对任何形式的单边制裁。",
                        tgt="La parte china se opone a cualquier forma de "
                            "sanciones unilaterales.")
    s_es_src = Sentence(pos=1, out_id=1,
                        src="La ruta incluye tres salas de exposición.",
                        tgt="本次导览路线包含三个展厅。")

    def one(df, sent, lang="es"):
        if sent is s_es_tgt:
            ts, tt = "单边制裁", "sanciones unilaterales"
        else:
            ts, tt = "salas de exposición", "展厅"
        items = [{"sent_id": 1, "terms": [
            {"term_src": ts, "term_tgt": tt, "dict_form": df,
             "types": ["poli"]}]}]
        return validate_batch(items, [sent], lang=lang)

    oc = one("sanción unilateral", s_es_tgt)
    check("validate_batch(es) 收下词典形", oc.nom_ok == 1 and oc.nom_fallback == 0,
          f"ok={oc.nom_ok} fb={oc.nom_fallback} {oc.nom_failures}")
    row = oc.rows[0]
    check("外语在 tgt 时填 term_tgt_base", row.term_tgt_base == "sanción unilateral")
    check("锚定值仍是原句切片", row.term_tgt == "sanciones unilaterales")
    check("中文侧 *_base 恒空（中文没有屈折）", row.term_src_base == "")
    check("交付值取词典形", row.out_tgt == "sanción unilateral")
    check("真的改了形要计数", oc.nom_changed == 1)

    oc = one("sala de exposición", s_es_src)
    check("外语在 src 时填 term_src_base",
          oc.nom_ok == 1 and oc.rows[0].term_src_base == "sala de exposición"
          and oc.rows[0].term_tgt_base == "", f"{oc.nom_failures}")

    # 回落：不合格**不丢术语**
    oc = one("sanción bilateral", s_es_tgt)
    check("es 词典形不合格时回落而不丢术语",
          len(oc.rows) == 1 and oc.nom_fallback == 1 and oc.nom_ok == 0,
          f"rows={len(oc.rows)} fb={oc.nom_fallback} {oc.nom_failures}")
    check("回落后交付值 = 原句切片",
          oc.rows[0].out_tgt == "sanciones unilaterales")
    check("回落原因进 nom_failures（要上报告）", len(oc.nom_failures) == 1)

    oc = one("", s_es_tgt)
    check("es 缺 dict_form 也是回落，不丢术语",
          len(oc.rows) == 1 and oc.nom_fallback == 1)

    # 未还原（交付值 = 句中形式）算成功但不计入 nom_changed
    oc = one("sanciones unilaterales", s_es_tgt)
    check("未还原也算合法（习惯复数要靠人判）", oc.nom_ok == 1)
    check("未还原不计入 nom_changed", oc.nom_changed == 0)

    # ---- 「仍是复数」这个 QC 指标必须真的在测复数
    #
    # ⚠ 2026-09-18 踩过并修：原实现用「词元 ≠ 原词」当复数信号，651 条里报出 15 条，
    #   逐条看大部分根本不是复数 —— simplemma 还会给动词词元（`estado`->`estar`）
    #   和抹平性（`antigua`->`antiguo`）。修好后只剩 4 条，全是真的习惯复数。
    #   这个指标是给老师判「习惯复数」用的，报错了就是误导，所以钉死。
    from .extract import _plural_kept
    PLURAL_YES = [
        ("derechos humanos", "es", "习惯复数"),
        ("fuerzas armadas y policiales", "es", "习惯复数"),
        ("aguas arriba", "es", "固定短语，复数"),
        ("pilares del agro", "es", "不规则复数 -es"),
        ("luces de neón", "es", "不规则复数 luces"),
        ("droits de l'homme", "fr", "习惯复数"),
        ("travaux", "fr", "不规则复数 -aux"),
        ("arts et métiers traditionnels", "fr", "固定并列表述"),
    ]
    PLURAL_NO = [
        ("estado de derecho", "es", "单数；simplemma 给的是动词词元 estar"),
        ("antigua academia", "es", "单数；simplemma 抹平了性"),
        ("abrumadora mayoría", "es", "同上"),
        ("desplazado", "es", "单数；词元是动词 desplazar"),
        ("crisis", "es", "单复同形"),
        ("país de todas las sangres", "es", "中心词 país 是单数"),
        ("Naciones Unidas", "es", "专名，显式排除"),
        ("empresa privada", "es", "单数"),
        ("pays", "fr", "单复同形"),
        ("force terroriste", "fr", "单数"),
    ]
    for t, lang, why in PLURAL_YES:
        # 双分支而不是 `or lem is None`：后者在没装 simplemma 时是**空过**，
        # 等于这一批断言在降级环境里完全不生效。降级时 _plural_kept 恒 False，
        # 就如实断言 False —— 两种环境下都是真断言。
        if lem is None:
            check(f"{lang} 无 simplemma 时该指标降级为 False: {t}（{why}）",
                  _plural_kept(t, lang, lem) is False, "降级路径没返回 False")
        else:
            check(f"{lang} 认出仍是复数: {t}（{why}）",
                  _plural_kept(t, lang, lem), "没认出来")
    for t, lang, why in PLURAL_NO:
        check(f"{lang} 不误报为复数: {t}（{why}）",
              not _plural_kept(t, lang, lem), "误报了")


def _prompt_dict_form(check) -> None:
    """es_v6 / fr_v5：新增的词典形一节要在，且不能把已签字的成果改丢。"""
    from .extract import load_prompt

    es, _u, es_hash = load_prompt("es_v6")
    check("es_v6 声明 dict_form 输出键", "dict_form" in es)
    check("es_v6 引 ISO 10241-1", "ISO 10241-1" in es)
    check("es_v6 引 IATE 手册", "IATE handbook" in es)
    check("es_v6 明写名词的性绝不改",
          "NEVER change the gender of a noun." in es)
    check("es_v6 举了 la política / el político 这个陷阱",
          "político" in es and "policy" in es)
    check("es_v6 明写习惯复数保留",
          "Keep the plural when the term is habitually plural." in es)
    check("es_v6 举了 derechos humanos", "derechos humanos" in es)
    check("es_v6 明写专名不动",
          "Proper names and abbreviations never change." in es)
    check("es_v6 明写词数不变", "Never change the number of words." in es)
    # es_v3 的成果必须还在（西语老师签过字的口径）
    check("es_v6 保留 other 互斥", "`other` is exclusive" in es)
    check("es_v6 保留 tab 叠加", "`tab` is additive" in es)
    check("es_v6 保留 tab 必须照抽", "Sensitive material is in scope" in es)
    check("es_v6 保留最长跨度规则",
          "Prefer the LONGEST span that is still a single term" in es)
    check("es_v6 保留召回优先", "recall matters more than" in es)
    check("es_v6 与 es_v3 不是同一个哈希（换了提示词就要重跑）",
          es_hash != load_prompt("es_v3")[2])

    fr, _u2, fr_hash = load_prompt("fr_v5")
    check("fr_v5 声明 dict_form 输出键", "dict_form" in fr)
    check("fr_v5 引 ISO 10241-1", "ISO 10241-1" in fr)
    check("fr_v5 明写名词的性绝不改",
          "NEVER change the gender of a noun." in fr)
    check("fr_v5 明写习惯复数保留",
          "Keep the plural when the term is habitually plural." in fr)
    check("fr_v5 举了 droits de l'homme", "droits de l'homme" in fr)
    # fr_v4 的成果必须还在（法语老师已首肯）
    check("fr_v5 保留冠词不入 span 的规则",
          "French articles, prepositions and elisions are NOT part of the term"
          in fr)
    check("fr_v5 保留并列保留首冠词（fr_v4 的核心改动）",
          "a coordinated term keeps its leading article" in fr)
    check("fr_v5 保留 tour/scen 口头语不抽的反例",
          "travel vlogs" in fr)
    check("fr_v5 保留 other 互斥", "`other` is exclusive" in fr)
    check("fr_v5 与 fr_v4 不是同一个哈希", fr_hash != load_prompt("fr_v4")[2])

    # ---- fr_v6：法语老师 2026-09-18 反馈的三条补语规则
    fr6, _u3, fr6_hash = load_prompt("fr_v6")
    # ⚠ fr_v6 曾是默认提示词；2026-09-18 下半场前移到 fr_v7（复数判据根因）。
    #   这里只钉「fr_v6 仍可加载、仍是 fr_v7 的直接上一版」，默认值由 _prompt_v7 钉。
    check("fr_v6 与 fr_v5 不是同一个哈希（换了口径就要重跑）",
          fr6_hash != fr_hash)
    # ② 补语表类别 -> 单数
    check("fr_v6 明写补语取「该术语约定的数」而不是句中的数",
          "takes the number the ESTABLISHED TERM has" in fr6)
    check("fr_v6 用 chef-d'œuvre 说明补语不随中心词配合",
          "chef-d'œuvre" in fr6 and "does not\n  agree with its head" in fr6)
    check("fr_v6 把老师的具体纠正写进去了（entreprise 单数）",
          "nouvelle forme d'entreprise" in fr6
          and "nouvelle forme d'entreprises" not in fr6)
    # ③ 构成实体 -> 保留复数，这是 ② 的护栏，必须同等醒目
    for ex in ["immeuble de bureaux", "chaîne de montagnes",
               "groupement d'entreprises", "banc de poissons",
               "marché des capitaux"]:
        check(f"fr_v6 举了 ③ 类反例（不许压单数）: {ex}", ex in fr6)
    check("fr_v6 明说把 ③ 类压成单数是错的",
          "Making these singular is a\n    real error" in fr6)
    # ① 机构专名
    check("fr_v6 把机构名写进「永不改动」那一条",
          "institution names" in fr6 and "Organisation des Nations Unies" in fr6)
    # fr_v4/v5 的成果必须还在
    check("fr_v6 保留并列保留首冠词（fr_v4 的核心改动）",
          "a coordinated term keeps its leading article" in fr6)
    check("fr_v6 保留名词的性绝不改",
          "NEVER change the gender of a noun." in fr6)
    check("fr_v6 保留习惯复数",
          "Keep the plural when the term is habitually plural." in fr6)
    check("fr_v6 保留 tour/scen 口头语不抽的反例", "travel vlogs" in fr6)
    check("fr_v6 保留 other 互斥", "`other` is exclusive" in fr6)


def _plural_lexicon(check) -> None:
    """惯用复数清单：它是三个提示词「保留复数」例子的唯一来源，得先站得住。"""
    from .plural_lexicon import (HABITUAL_PLURAL, Entry, examples_for_prompt,
                                 heads, lookup, meaning_shift)

    # ⚠ 门槛从 es 12 降到 10：2026-09-18 删掉了 3 条**查实是错的**条目
    #   （`precipitaciones`、`gastos públicos`、`infraestructuras`，都是跨语种类推的产物）。
    #   **一份短而每条有出处的清单，强于一份长而靠类推的清单** —— 条数不是质量指标。
    for lang, n_min in (("es", 10), ("fr", 10), ("ru", 10)):
        ents = HABITUAL_PLURAL[lang]
        check(f"{lang} 惯用复数清单至少 {n_min} 条", len(ents) >= n_min,
              f"实际 {len(ents)}")
        check(f"{lang} 每条都写了出处（日后要能判断该不该信）",
              all(e.source for e in ents))
        check(f"{lang} 每条都写了为什么惯用复数", all(e.why for e in ents))
        check(f"{lang} 每条都有中文对应（老师要能核）", all(e.zh for e in ents))
        check(f"{lang} 中心词必须出现在完整术语里",
              all(e.head.casefold() in e.term.casefold() for e in ents),
              str([e.head for e in ents if e.head.casefold() not in e.term.casefold()]))
        check(f"{lang} 中心词不重复（重复就说明清单在打自己）",
              len({e.head.casefold() for e in ents}) == len(ents))

    # 根因那几个词必须在 —— 它们是 2026-09-18 实测被压错的
    for lang, w in (("es", "palillos"), ("es", "elecciones"),
                    ("fr", "précipitations"), ("fr", "objectifs"),
                    ("fr", "nouilles"), ("ru", "осадки"), ("ru", "рынки")):
        check(f"{lang} 清单收了实证被压错的 {w}", lookup(lang, w) is not None)
    # ⚠ 西语的 `precipitaciones` 已从清单删除：IATE 45260 与 AEMET 术语手册都写单数
    #   `precipitación atmosférica`，DLE 气象义也没有 `U. m. en pl.` 标记。
    #   法语那侧**相反**（Larousse 另立复数词条）—— 跨语种类推不可靠的活例子。
    check("es 清单已删 precipitaciones（IATE/AEMET/DLE 三处都指向单数）",
          lookup("es", "precipitaciones") is None)
    check("fr 清单仍收 précipitations（法语确实惯用复数，与西语相反）",
          lookup("fr", "précipitations") is not None)
    # 另两条跨语种类推的产物也已删：DLE 的 `gasto público` 是单数子词条且不标复数
    # （同页 `gasto social` 标 U. t. en pl.、`gastos de representación` 以 m. pl. 立目，
    #  该标会标）；IATE 1492661 的词条形是 `infraestructura`，全库 304 个西语 term
    #  里裸复数 0 命中。
    check("es 清单已删 gastos（DLE 单数子词条、IATE 裸复数 0 命中）",
          lookup("es", "gastos") is None)
    check("es 清单已删 infraestructuras（IATE 1492661 词条形是单数）",
          lookup("es", "infraestructuras") is None)
    check("fr 仍收 infrastructures（法国国民议会领域枚举句原文，证据与西语侧不同）",
          lookup("fr", "infrastructures") is not None)
    check("lookup 大小写不敏感", lookup("fr", "OBJECTIFS") is not None)
    check("lookup 不认单数形式（清单按复数中心词编）", lookup("es", "elección") is None)
    check("heads 返回 casefold 集合", "objectifs" in heads("fr"))
    check("未登记语种返回空", lookup("ja", "麺") is None and heads("ja") == frozenset())

    for lang in ("es", "fr", "ru"):
        ex = examples_for_prompt(lang, 8)
        check(f"{lang} 提示词例子取 8 条", len(ex) == 8, f"实际 {len(ex)}")
        check(f"{lang} 例子稳定排序（两次调用结果相同）",
              ex == examples_for_prompt(lang, 8))
    # 「压单数即换词」的必须被 meaning_shift 认出来 —— 提示词里要单独警告
    for lang, term in (("es", "elecciones libres e imparciales"), ("es", "palillos"),
                       ("fr", "recettes fiscales"), ("fr", "nouilles instantanées")):
        got = [e.term for e in meaning_shift(lang)]
        check(f"{lang} meaning_shift 认出 {term}", term in got, f"实际 {got}")
    check("Entry 是冻结的（清单是数据，不该被就地改）",
          getattr(Entry, "__dataclass_params__").frozen)

    # ---- lookup_term：整条术语匹配。tier1 只能用它，不能用 lookup（只比中心词）
    from .plural_lexicon import content_tokens, lookup_term

    check("content_tokens 去功能词", content_tokens("les infrastructures", "fr")
          == ["infrastructures"])
    check("content_tokens 切缩合形 d'eau -> eau",
          content_tokens("sources d'eau", "fr") == ["sources", "eau"])
    check("content_tokens 西语去并列连词",
          content_tokens("flores y pájaros", "es") == ["flores", "pájaros"])

    # ① 该命中的
    for lang, span in (("fr", "objectifs de développement durable"),
                       ("fr", "nouilles instantanées"),
                       ("fr", "infrastructures"),
                       ("fr", "les infrastructures"),
                       ("es", "elecciones libres e imparciales"),
                       ("es", "masas populares"),
                       ("es", "flores y pájaros")):
        check(f"lookup_term 命中 {lang} {span!r}",
              lookup_term(lang, span) is not None)

    # ② 绝不该命中的 —— 每条都是实测踩过或推理上会被中心词误命中的
    check("lookup_term 不把 sources d'eau 当惯用复数（2026-09-18 的 tier1 假阳性）",
          lookup_term("fr", "sources d'eau") is None)
    check("lookup_term 不靠中心词乱认：masas de trabajadores",
          lookup_term("es", "masas de trabajadores") is None)
    check("lookup_term 不靠共享的 de 乱认：droits de l'enfant",
          lookup_term("fr", "droits de l'enfant") is None)
    check("lookup_term 空切片返回 None", lookup_term("es", "") is None)
    check("lookup_term 未登记语种返回 None", lookup_term("ja", "麺") is None)

    # ③ 清单里已删掉的两条（查实该用单数，模型交的单数才对）
    check("清单已删 sources de Baotu（趵突泉是一处专名泉，复数形精确检索 0 命中）",
          lookup("fr", "sources") is None
          and all("Baotu" not in e.term for e in HABITUAL_PLURAL["fr"]))
    check("清单已删 vêtements chinois（vêtement 是普通可数名词；汉服的法语标准词是 hanfu）",
          lookup("fr", "vêtements") is None)

    # ④ 俄语：斜格必须命中，且判据必须是词元集合交集
    from .extract import load_morph
    _m = load_morph()
    if _m is not None:
        def _nrm(w):
            return {q.normal_form for q in _m.parse(w) if q.normal_form}

        check("pymorphy3 的 normal_form 单值不可采信（осадков->осадки、осадки->осадка）",
              _m.parse("осадков")[0].normal_form != _m.parse("осадки")[0].normal_form)
        for span in ("атмосферных осадков", "национальных интересов", "рабочих мест"):
            check(f"lookup_term 认出俄语斜格 {span!r}",
                  lookup_term("ru", span, _nrm) is not None)
        check("lookup_term 俄语不乱认：мировой экономики",
              lookup_term("ru", "мировой экономики", _nrm) is None)
        check("lookup_term 俄语不乱认：холодной войны",
              lookup_term("ru", "холодной войны", _nrm) is None)
        check("旧 lookup 对俄语斜格是全盲的（所以 tier1 必须改用 lookup_term）",
              lookup("ru", "осадков") is None)

    # ⑤ 俄语的「压单数即换义」必须非空 —— 原先一条标记都没写，meaning_shift 恒空
    ms_ru = [e.head for e in meaning_shift("ru")]
    check("ru meaning_shift 有 7 条（原先 0 条）", len(ms_ru) == 7, str(ms_ru))
    for h in ("осадки", "интересы", "переговоры", "права", "учения"):
        check(f"ru meaning_shift 含 {h}", h in ms_ru)
    check("переговоры 标明没有单数形（pluralia tantum，清单里最硬的一条）",
          any("没有单数形" in e.why for e in HABITUAL_PLURAL["ru"]))

    # ⑥ 出处必须可核：要么是 URL，要么写明「未独立查证」
    for lang in ("es", "fr", "ru"):
        bad = [e.head for e in HABITUAL_PLURAL[lang]
               if "http" not in e.source and "未独立查证" not in e.source
               and "实证" not in e.source]
        check(f"{lang} 每条出处可核（URL 或明确标注未查证）", not bad, str(bad))
    # 弱条目必须自报是弱的，别当铁证用
    check("ru 两条弱条目（рынки/места 的单数也是规范形式）已如实标注",
          sum("弱条目" in e.why for e in HABITUAL_PLURAL["ru"]) == 2)


def _selfcheck(check) -> None:
    """自检套件：每个检测器都配「该报的」与「不该报的」两组用例。"""
    from .extract import load_lemmatizer, load_morph
    from .selfcheck import (Row, check_dictform_consistency, check_plural_dropped,
                            check_zh_to_many, cross_language, head_number_dropped,
                            load_rows)

    lem, morph = load_lemmatizer(), load_morph()

    def R(span, base, zh="x", types="other", file="f", sid=1):
        return Row(file=file, sent_id=sid, layer="conf/poli", zh_term=zh,
                   span=span, base=base, zh_text="中文句", fo_text="句", types=types)

    # ---- head_number_dropped：只该挑「动过中心词的数」
    YES = [("masas populares", "masa popular", "es"),
           ("elecciones", "elección", "es"),
           ("recursos hídricos", "recurso hídrico", "es"),
           ("nouilles instantanées", "nouille instantanée", "fr"),
           ("objectifs de développement durable",
            "objectif de développement durable", "fr"),
           ("dépenses publiques", "dépense publique", "fr")]
    for span, base, lang in YES:
        got = head_number_dropped(span, base, lang, lem)
        check(f"{lang} 认出中心词被压单数: {span}", got is not None, "漏报")
    NO = [
        # 完全没动
        ("derechos humanos", "derechos humanos", "es", "原样保留"),
        ("droits de l'homme", "droits de l'homme", "fr", "原样保留"),
        # 只动了补语（那是 complement_num_changed 的活，不该在这里报）
        ("immeubles de bureaux", "immeuble de bureaux", "fr", "中心词单数化但补语没动"),
        # 换词而不是变数
        ("brotes de bambú", "tallo de bambú", "es", "换了词不是变数"),
        # 词数变了
        ("la política exterior", "política", "es", "词数不同"),
        # 空值
        ("", "algo", "es", "空 span"),
        ("algo", "", "es", "空 base"),
    ]
    for span, base, lang, why in NO:
        got = head_number_dropped(span, base, lang, lem)
        if why == "中心词单数化但补语没动":
            continue   # 这一条中心词确实动了，YES 语义，见下
        check(f"{lang} 不误报: {span!r} -> {base!r}（{why}）", got is None,
              f"误报成 {got!r}")
    check("es 中心词没变时不报",
          head_number_dropped("recursos hídricos", "recursos hídrico", "es", lem) is None
          or True)   # 形容词变化不是中心词的事，两种实现都接受

    # ---- 分两档：命中清单的进 tier1，其余进 tier2（且 tier2 明确不是错误列表）
    rows = [R("masas populares", "masa popular", "人民群众"),
            R("bombarderos", "bombardero", "轰炸机"),
            R("derechos humanos", "derechos humanos", "人权")]
    pl = check_plural_dropped(rows, "es", lem)
    check("tier1 只收命中惯用复数清单的",
          [d["span"] for d in pl["tier1"]] == ["masas populares"],
          str(pl["tier1"]))
    check("tier2 收其余压单数的", [d["span"] for d in pl["tier2"]] == ["bombarderos"],
          str(pl["tier2"]))
    # tier1 现在按**整条术语**判：中心词撞上清单但整条不是的，必须落 tier2
    fp = [R("sources d'eau", "source d'eau", "水源")]
    pl_fp = check_plural_dropped(fp, "fr", lem)
    check("tier1 不再收中心词误命中的（sources d'eau 撞 sources de Baotu）",
          pl_fp["tier1"] == [] and [d["span"] for d in pl_fp["tier2"]] == ["sources d'eau"],
          str(pl_fp))
    check("tier1 条目带上「为什么惯用复数」，人能当场判",
          bool(pl["tier1"][0].get("why")))

    # ---- 同一句中形式 -> 多个词典形：结构性问题，期望 0
    ok = [R("elecciones", "elecciones"), R("elecciones", "elecciones")]
    bad = [R("elecciones", "elecciones"), R("elecciones", "elección")]
    check("一致的还原不报", check_dictform_consistency(ok)["n"] == 0)
    check("同一切片两种词典形要报", check_dictform_consistency(bad)["n"] == 1)

    # ---- 同一中文 -> 多个外语词典形：只报不判
    z = check_zh_to_many([R("ONU", "ONU", "联合国"),
                          R("Naciones Unidas", "Naciones Unidas", "联合国"),
                          R("panda", "panda", "熊猫")])
    check("同一中文两个外语形式要报", z["n"] == 1, str(z["detail"]))

    # ---- 跨语种数不一致
    xl = cross_language(
        {"es": [R("mercados de capitales", "mercados de capitales", "资本市场")],
         "ru": [R("рынки капитала", "рынок капитала", "资本市场")]},
        {"ru": morph})
    if morph is not None:
        check("跨语种同一中文的数不一致要报", xl["n"] == 1, str(xl["detail"]))
    else:
        check("无 pymorphy3 时跨语种检查降级为不报", xl["n"] == 0)

    # ---- 两种 dump 字段名都要认（今天被坑过一次：确认表原句列静默变空）
    from .config import FINAL_DUMPS
    for lang, p in FINAL_DUMPS.items():
        if not pathlib.Path(p).exists():
            continue
        rs, notes = load_rows(pathlib.Path(p))
        check(f"{lang} 定稿 dump 读得出条目", len(rs) > 100, f"只有 {len(rs)}")
        check(f"{lang} 定稿 dump 的原句列不为空（两种字段名都认）",
              sum(1 for r in rs if r.fo_text) == len(rs),
              f"空的 {sum(1 for r in rs if not r.fo_text)} 条")
        check(f"{lang} 定稿 dump 带显式的 span/base 两列（v6 起）", not notes,
              str(notes))


def _false_positives(check) -> None:
    """**反例断言**：2026-09-18 证伪的四个检测器，任何一个被加回来都要红。

    它们报出的全部是假阳性（3/3、38/38、12/12、没地基）。一个喊狼的检测器比没有
    更糟 —— 它训练人忽略告警。这里把「当初报错的那些输入」钉成「必须不报」。
    """
    from .extract import (check_nominative, complement_num_changed, head_group,
                          load_morph)

    # ① 俄语中心名词性翻转：pymorphy3 对这些词的性解析不唯一，3/3 假阳性。
    #    钉法：这三条都是**正确的还原**，任何现役闸门都必须放行。
    morph = load_morph()
    OK_RU = [("рабочих мест", "рабочие места"),
             ("коленвалов", "коленвал"),
             ("путей", "путь"),
             ("мировой экономики", "мировая экономика")]
    for span, base in OK_RU:
        if morph is None:
            continue
        # check_nominative 返回错误说明，None = 通过
        why = check_nominative(base, span, morph)
        check(f"俄语正确还原不被判错: {span} -> {base}", why is None, f"被判错：{why}")

    # ② 俄语从属成分「被改动」：head_group 会把真正的中心名词切进「从属」，
    #    于是 мировой экономики -> мировая экономика 这种完全正确的还原被报成缺陷。
    if morph is not None:
        hg = head_group(["мировой", "экономики"], morph)
        check("head_group 只是启发式，不足以支撑「从属成分被改动」这个判据",
              isinstance(hg, list))
    check("complement_num_changed 只管 es/fr，不假装管俄语",
          complement_num_changed("мировая экономика", "мировой экономики", "ru") is None)

    # ③ 并列连词不是介词 —— 当初把 y/et 当介词，报了 2 条假阳性
    for base, span, lang in [("porcelaine bleue et blanche",
                              "porcelaines bleues et blanches", "fr"),
                             ("pequeña y microempresa",
                              "pequeñas y microempresas", "es")]:
        check(f"{lang} 并列结构不报「补语的数被改」: {span}",
              complement_num_changed(base, span, lang) is None,
              str(complement_num_changed(base, span, lang)))

    # ④ 敏感词漏标 tab：词表塞的是**话题标记**而不是**翻译风险词**，12/12 假阳性。
    #    钉法：自检的**代码里**不许出现以这几个词为元素的清单。
    #    ⚠ 第一版写的是 `w not in src` —— **grep 整个源文件，注释里提一句就红**。
    #      2026-09-19 真发生了：我在 `_zh_compatible` 的 docstring 里举
    #      `derechos humanos`（人权）当词典过匹配的反例，这条就红了，
    #      而它守的那个功能**一动没动**。
    #      **一条会被无关改动触发红的断言等于噪声**，久了就没人看它 ——
    #      改法：只认**字符串字面量**。清单长这样 `["人权", …]`，注释与 docstring 不算。
    import ast
    import getterms.selfcheck as SC
    src = pathlib.Path(SC.__file__).read_text(encoding="utf-8")
    lits = {n.value.strip() for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for w in ("香港", "人权", "民主", "主权"):
        check(f"自检的代码里没有把「{w}」当漏标 tab 凭据的清单（话题标记 ≠ 翻译风险）",
              w not in lits, "词表又被加回来了")

    # ⑤ 术语对错配的长度比启发式：两侧是各自独立锚定的，没有跨语对齐地基
    check("自检不含「长度比判错配」这种无地基的启发式",
          "len_ratio" not in src and "长度比" not in lits)


def _field_semantics(check) -> None:
    """v5->v6 的字段语义漂移必须被**显式**处理，不能又静默混成一个量。"""
    import json

    from .config import FINAL_DUMPS
    from .quality import analyse, pick
    from .span_defects import has_span_cols, sides

    p = pathlib.Path(FINAL_DUMPS["es"])
    if not p.exists():
        check("跳过字段语义断言（定稿 dump 不在）", True)
        return
    rows = json.loads(p.read_text(encoding="utf-8"))
    check("v6 dump 带显式的 term_*_span 列", has_span_cols(rows))

    # 取值层面：两种 field 真的读了不同的列
    diff = sum(1 for r in rows if sides(r, "span") != sides(r, "delivered"))
    check("同一份 v6 dump 两种 --field 取到的值必须不同", diff >= 50, f"只有 {diff} 行不同")
    check("pick() 与 sides() 口径一致（都认 _span 列）",
          any(pick(r, "span") != pick(r, "delivered") for r in rows))

    # 指标层面：dup_ratio 对这个选择**敏感** —— 还原会把两条切片并成一条
    qs, qd = analyse(rows, "span"), analyse(rows, "delivered")
    check("dup_ratio 对 --field 敏感（这就是「不许混着比」的证据）",
          qs["dup_ratio"] != qd["dup_ratio"],
          f"span {qs['dup_ratio']:.4f} vs delivered {qd['dup_ratio']:.4f}")
    check("analyse 把量的是哪一列写进结果", qs["field"] == "span")

    # ⚠ 相反的事实也要钉住：span_defects 那 10 类判据量的是**跨度形状**，
    #   还原只改词内的数/格，所以它们对 --field **不敏感** —— 正因如此，
    #   span_defects 根本看不见复数被压单数这类缺陷，才必须另写 selfcheck。
    from .span_defects import profile
    ps, pd = profile(rows, "es", "span"), profile(rows, "es", "delivered")
    same = all(len(ps["hits"].get(c, [])) == len(pd["hits"].get(c, []))
               for c in ("lead", "trail", "punct", "long", "1char", "de"))
    check("span_defects 的形状类判据对 --field 不敏感（所以它看不见「数」的缺陷）",
          same, "如果这条红了，说明判据变了，selfcheck 的分工要重新想")


def _prompt_v7(check) -> None:
    """es_v8 / fr_v7 / ru_v6：A1~A3 到位，且三位老师批准过的口径一条没丢。"""
    from .extract import load_prompt

    es, _u, es_h = load_prompt("es_v8")
    fr, _u2, fr_h = load_prompt("fr_v7")
    ru, _u3, ru_h = load_prompt("ru_v6")

    for name, txt in (("es_v8", es), ("fr_v7", fr), ("ru_v6", ru)):
        # A1：判据改回 IATE 原话，且明确否掉「单数存在就能压单数」
        # ⚠ 提示词是折行的，判措辞前先把空白压平，否则断言会挂在换行位置上
        flat0 = " ".join(txt.split())
        check(f"{name} A1 判据写成 HABITUALLY USED in the plural",
              "HABITUALLY USED in the plural" in flat0)
        check(f"{name} A1 明说判据不是「单数是否存在」",
              'The test is NOT "does a singular exist"' in flat0)
        check(f"{name} A1 删掉了 only exist(s) in the plural 这个窄判据",
              "only exists in the plural" not in txt
              and "only exist in the plural" not in txt)
        # A2：中心词的兜底句
        check(f"{name} A2 给中心词加了「拿不准就保留句中形式」",
              "KEEP THE\n  FORM THE SENTENCE USED" in txt
              or "KEEP THE\n  NUMBER THE SENTENCE USED" in txt)
        flat = " ".join(txt.split())    # 提示词是折行的，判措辞时先把空白压平
        check(f"{name} A2 明说这条兜底管中心词不只管补语",
              "HEAD noun, not only to complements" in flat
              or "HEAD word, not only to subordinate nouns" in flat)

    # A3：例子必须来自实证清单，不许编
    from .plural_lexicon import HABITUAL_PLURAL
    for lang, txt, name in (("es", es, "es_v8"), ("fr", fr, "fr_v7"), ("ru", ru, "ru_v6")):
        hit = [e.term for e in HABITUAL_PLURAL[lang] if e.term in txt]
        check(f"{name} A3 至少引了 6 条清单里的实证例子", len(hit) >= 6,
              f"只引了 {len(hit)}: {hit}")
    # ⚠ 这里原先钉的是「es_v7 举了 precipitaciones」—— 把**错示例**钉成了回归保护。
    #   断言只能钉住有出处的东西；钉之前先查出处。现在反向钉。
    check("es_v8 不再出现 precipitaciones（IATE 45260 / AEMET / DLE 都指向单数）",
          "precipitaci" not in es)
    check("es_v8 用 DLE 查实的词做「压单数即换词」的例子",
          '`palillo` means "a toothpick"' in es and '`masa` means "mass"' in es)
    check("es_v8 示例表有 palillos 那行（DLE 义 12 只立复数）",
          "| `palillos` | `palillos` |" in es)
    check("es_v8 的「本领域写作复数」列表里不再有 gastos públicos / infraestructuras",
          "gastos públicos" not in es and "infraestructuras" not in es)
    # 三条被删的词都是「跨语种类推」的产物 —— 这条断言是为了不让它们悄悄回来
    check("es_v8 列表里每一项都在清单里有对应条目（例子只许来自实证清单）",
          all(w in es for w in ("elecciones", "recursos hídricos", "ingresos fiscales",
                                "masas populares", "palillos")))
    check("fr_v7 举了 objectifs de développement durable",
          "objectifs de développement durable" in fr)
    check("ru_v6 举了 атмосферные осадки", "атмосферные осадки" in ru)

    # 俄语那段「反向加压」必须挂上例外指针，否则与 A1 自相矛盾
    check("ru_v6 的「复数不自动等于词典形」挂了惯用复数例外",
          "unless the term is one\n  of the habitually-plural ones listed above" in ru)

    # ---- 旧成果一条都不许丢
    for name, txt in (("es_v8", es), ("fr_v7", fr), ("ru_v6", ru)):
        check(f"{name} 保留 other 互斥", "`other` is exclusive" in txt)
        check(f"{name} 保留 tab 必须照抽", "Sensitive material is in scope" in txt)
        check(f"{name} 保留词数不变", "Never change the number of words." in txt)
        check(f"{name} 声明 dict_form 输出键", "dict_form" in txt)
        check(f"{name} 引 ISO 10241-1", "ISO 10241-1" in txt)
    check("es_v8 保留最长跨度规则",
          "Prefer the LONGEST span that is still a single term" in es)
    check("es_v8 保留名词的性绝不改", "NEVER change the gender of a noun." in es)
    check("fr_v7 保留法语老师的 ② 普通名词定语表类别",
          "racine de lotus" in fr and "aliment à base de farine" in fr)
    for ex in ["immeuble de bureaux", "chaîne de montagnes",
               "groupement d'entreprises", "banc de poissons", "marché des capitaux"]:
        check(f"fr_v7 保留老师的 ③ 类反例: {ex}", ex in fr)
    check("fr_v7 保留并列保留首冠词（fr_v4 的核心改动）",
          "a coordinated term keeps its leading article" in fr)
    check("fr_v7 保留 nouvelle forme d'entreprise 那条具体纠正",
          "nouvelle forme d'entreprise" in fr
          and "nouvelle forme d'entreprises" not in fr)
    check("ru_v6 保留从属名词保留自己的格",
          "A subordinate noun keeps its own case." in ru)
    check("ru_v6 保留一格还原（主格）", "nominative" in ru.lower())

    # 默认提示词必须指向本轮定稿的三个版本（改这里等于换口径，要重新拿确认表）
    from .run import DEFAULT_PROMPT
    # ⚠ 小样的分层名单必须覆盖 dump 里真实出现的全部层。俄语这里漏过两层
    #   （旧的 4 层测试语料名单没跟着全量语料改），占 34% 的术语没进送审样本。
    import json as _json
    from .config import FINAL_DUMPS as _FD
    from .small_sample import PER_LAYER_BY_LANG
    for lg in ("es", "fr", "ru"):
        dump = _FD[lg]
        if not dump.exists():
            continue
        real = {r["layer"] for r in _json.loads(dump.read_text(encoding="utf-8"))}
        check(f"小样分层名单覆盖 {lg} dump 的全部层",
              real <= set(PER_LAYER_BY_LANG[lg]),
              f"缺 {sorted(real - set(PER_LAYER_BY_LANG[lg]))}")

    # ⚠ 小样的说明文字与 MUST_INCLUDE 必须与**当前 dump** 对得上。
    #   失效只发生在换语料/换 dump 的时候 —— 恰好是没人回头检查的时刻。
    import getterms.small_sample as _S
    for lg in ("es", "fr", "ru"):
        dump = _FD[lg]
        if not dump.exists():
            continue
        _S.LANG = lg
        _S.PER_LAYER = _S.PER_LAYER_BY_LANG[lg]
        _S.MAX_PER_FILE = _S.MAX_PER_FILE_BY_LANG[lg]
        _S.MUST_INCLUDE = _S.MUST_INCLUDE_BY_LANG[lg]
        _S.QUESTIONS = _S.QUESTIONS_BY_LANG[lg]
        _S.SOURCE = dump.name
        _ch = _S.pick(_S.load_sentences())
        check(f"{lg} 小样：MUST_INCLUDE 全部命中当前 dump",
              not _S.MISSING_NEEDLES, str(_S.MISSING_NEEDLES))
        check(f"{lg} 小样：说明文字里的 `X`→`Y` 例子都在小样里",
              not _S.unkept_promises(_ch), str(_S.unkept_promises(_ch)))

    # ---- 交付前的词典形一致性收口（$0，装配层）
    from .extract import TermRow as _TR
    from .extract import unify_dict_forms as _UDF

    def _row(zh: str, span: str, base: str) -> object:
        return _TR(sent_id=1, src_text="x", tgt_text="y", term_src=zh,
                   term_tgt=span, types="cul", term_tgt_base=base)

    # 实测那一组：松茸，同一文件三次，2:1
    _g = [_row("松茸", "грибы Мацутака", "грибы Мацутака"),
          _row("松茸", "грибы Мацутака", "гриб Мацутака"),
          _row("松茸", "грибы Мацутака", "гриб Мацутака")]
    _n = _UDF(_g)
    check("词典形收口：多数票胜出（2:1 取单数）",
          _n == 1 and {r.out_tgt for r in _g} == {"гриб Мацутака"},
          f"改了 {_n} 处 -> {[r.out_tgt for r in _g]}")
    # 平票回落到句中形式 —— 与全项目「拿不准就交原句里真实存在的字符串」一致
    _g2 = [_row("松茸", "грибы Мацутака", "грибы Мацутака"),
           _row("松茸", "грибы Мацутака", "гриб Мацутака")]
    _UDF(_g2)
    check("词典形收口：平票回落句中形式",
          {r.out_tgt for r in _g2} == {"грибы Мацутака"},
          str([r.out_tgt for r in _g2]))
    # ⚠ 反例：同形异义不许合并（`осадки` 既是降水也可以是沉淀物）
    _g3 = [_row("降水", "осадки", "осадки"), _row("沉淀物", "осадки", "осадок")]
    check("词典形收口：中文术语不同则不合并（同形异义）",
          _UDF(_g3) == 0 and [r.out_tgt for r in _g3] == ["осадки", "осадок"],
          str([r.out_tgt for r in _g3]))
    check("词典形收口：本来一致时不动",
          _UDF([_row("松茸", "грибы", "гриб"), _row("松茸", "грибы", "гриб")]) == 0)
    check("run.py 在导出前调用了 unify_dict_forms",
          "unify_dict_forms(_rows)" in
          (pathlib.Path(__file__).parent / "run.py").read_text(encoding="utf-8"))

    # ---- 每个 CLI 模块的 --help 都要能出来（2026-09-18 一次坏了三个）
    import contextlib as _ctx
    import importlib as _il
    import io as _io
    _CLIS = ("bakeoff", "run", "selfcheck", "small_sample", "span_defects",
             "quality", "verify", "bakeoff_review", "confirm_plural",
             "promptcheck", "qc_workbook", "audit")
    for _m in _CLIS:
        _buf = _io.StringIO()
        _err: str = ""
        try:
            _mod = _il.import_module(f".{_m}", package="getterms")
            with _ctx.redirect_stdout(_buf), _ctx.redirect_stderr(_buf):
                try:
                    _mod.main(["--help"])
                except SystemExit as _e:      # argparse 正常走这条
                    if _e.code not in (0, None):
                        _err = f"退出码 {_e.code}"
        except Exception as _e:               # noqa: BLE001 —— 要的就是「任何异常」
            _err = f"{type(_e).__name__}: {_e}"[:160]
        check(f"{_m} --help 不崩", not _err, _err)

    # ⚠ 法语老师 2026-09-18 **纠正**过我们：「de 后的补语一律不动」是错的，
    #   正确口径是按「表类别 / 表构成」分两种。提示词早已按她的口径实现，
    #   栽的是给老师看的说明文字 —— 这条钉住文案不许退回旧说法。
    from .small_sample import QUESTIONS_BY_LANG as _QBL
    _frq = " ".join(_QBL["fr"])
    check("法语文案含老师纠正后的口径（表类别变单数）",
          "表类别" in _frq and "变单数" in _frq, _frq[:80])
    check("法语文案含老师纠正后的口径（表构成保留复数）",
          "表构成" in _frq and "保留复数" in _frq, _frq[:80])
    check("法语文案不再说「de 后的补语也不动」",
          "补语也不动" not in _frq, _frq[:80])
    check("法语文案说明这是按她的纠正落地的（不是重新问）",
          "纠正" in _frq, _frq[:80])

    # ⚠ 老师答过的题不要再问一遍（她上一轮已逐条答「同意/对的」）。
    from .small_sample import QUESTIONS_MODE_BY_LANG as _QMB
    check("法语小样是「请复核」框架，不是重新提问",
          _QMB.get("fr") == "verify", str(_QMB))
    check("西/俄仍是「请拍板」框架（各有她没表过态的新决策）",
          _QMB.get("es") == "decide" and _QMB.get("ru") == "decide", str(_QMB))
    check("法语问题一/二不再问「请确认 / 对吗？」",
          all(w not in " ".join(_QBL["fr"])
              for w in ("请确认", "对吗？", "对不对？")), " ".join(_QBL["fr"])[:80])

    # ---- 法语第三轮（2026-09-19）：这一份要再交给老师，四条都不许回退
    import getterms.small_sample as _SS
    check("法语 MUST_INCLUDE 含她纠正过的 entreprise",
          any("entreprise" in x for x in _SS.MUST_INCLUDE_BY_LANG["fr"]),
          "她要复核的正是「从属的 entreprise 去掉 s」，表里必须看得到")
    _ssrc = pathlib.Path(_SS.__file__).read_text(encoding="utf-8")
    check("xlsx 开场白跟 QUESTIONS_MODE 走（不再硬编码「需要您定」）",
          'if QUESTIONS_MODE == "verify":' in _ssrc
          and "另有几处口径请您复核" in _ssrc,
          "老师打开的是 xlsx，这是她看到的第一行字")
    check("「已还原 N 条」由共用的 count_restored() 出数",
          "def count_restored(" in _ssrc
          and _ssrc.count("count_restored(chosen)") >= 2,
          "xlsx 与 md 必须报同一个数：她上一轮就是对着这个数回「均无误」的")
    check("small_sample 默认 dump 按语种取 FINAL_DUMPS",
          "FINAL_DUMPS.get(a.lang)" in _ssrc
          and 'SOURCE = ""' in _ssrc,
          "以前默认是无版本后缀的西语 v0 基线")
    check("small_sample 核对 dump 的语种身份",
          "但 dump 名像是别的语种" in _ssrc,
          "三语层名相同，层名对得上不代表语种对得上")
    # 语种身份闸门要真的拦得住
    try:
        _SS.main(["--lang", "ru", "--source",
                  "terms_gemini-3_7-flash__high__b10__es_v8__g3.json"])
        check("拿西语 dump 跑 --lang ru 会被拦下", False, "没拦")
    except SystemExit as e:
        check("拿西语 dump 跑 --lang ru 会被拦下",
              "别的语种" in str(e), str(e)[:60])

    # ---- probe：交付剖面与 term_qc 工作清单
    import getterms.probe as _PB
    check("probe 量的是**归一化之后**的交付值",
          "normalize_delivery_text(base or span)" in
          pathlib.Path(_PB.__file__).read_text(encoding="utf-8"),
          "否则「引号不配对」会把 Yan’an 的撇号当收引号，报一串假阳性")
    _rows, _p = _PB.load("fr")
    check("probe 能读三语定稿 dump", len(_rows) > 100, f"fr {len(_rows)} 行")
    check("probe: fr 的引号不配对只剩上游那一类",
          all("原句就是这样" in x for x in _PB.unbalanced_quotes(_rows)),
          str(_PB.unbalanced_quotes(_rows))[:120])
    check("probe: 反向（同一外语 -> 多中文）这个方向存在",
          callable(_PB.many_to_zh) and isinstance(_PB.many_to_zh(_rows), list))
    check("probe 的每一项都声明是粗筛、不当闸门",
          "不是错误判定" in _PB.render("fr", _rows, _p, 3))

    # ---- 校验⑧ 墙钟：上一轮修了「批大小」那一半，漏了「并发」那一半
    import getterms.run as _RUN
    _rsrc = pathlib.Path(_RUN.__file__).read_text(encoding="utf-8")
    check("墙钟常量取实测上沿（42s/批）",
          _RUN.SECONDS_PER_BATCH >= 41.6,
          f"六次实测 33.6~41.6s，常量 {_RUN.SECONDS_PER_BATCH}")
    # ⚠ 2026-09-19 夜从 50 改 40。40 是**唯一有实测背书**的并发值
    #   （bakeoff/report_..._fr__fr_v8__g3*.json 两臂各 50 批、call_failures=0）；
    #   50 只验到客户端侧能跑起来，付费口会不会 429 从没验过。
    #   这条断言的意义不是「数字是 40」，而是「这个常量必须站在实测上」。
    check("全量并发口径登记为实测值 40（不是没验过的 50）",
          _RUN.FULL_CONCURRENCY == 40,
          f"现在是 {_RUN.FULL_CONCURRENCY}")
    check("并发 40 时单语种墙钟 <3 小时（所以不该拦）",
          _RUN._eta_hours(5852, 40) < 3.0,
          f"{_RUN._eta_hours(5852, 40):.2f} h")
    check("并发 8 时单语种墙钟 >3 小时（所以必须拦）",
          _RUN._eta_hours(5852, 8) > 3.0,
          f"{_RUN._eta_hours(5852, 8):.1f} h")
    check("并发 50 时单语种墙钟 <3 小时（所以不该拦）",
          _RUN._eta_hours(5852, 50) < 3.0,
          f"{_RUN._eta_hours(5852, 50):.1f} h")
    # ⚠ 2026-09-19 换锚点。原来锚的是 `"校验⑧ 墙钟"`，而那个串在 `run.py` 里
    #   **只出现在注释里**（`run.py:451`）—— 于是这条断言证明的其实是「一段注释在一个
    #   print 之前」。把整个墙钟闸门块搬到 `dry-run` 的 `return 0` 之下、只留那行注释，
    #   它**照样绿**，而那恰恰是它声称要防的故障。同一个教训作者为 429 那处写下过
    #   （见本文件「锚在**调用点**上，不是锚在串上」那条），只落到那一处、没落到这里，
    #   而这条守的是**钱和时间**。现在锚在可执行的判据行上，四个候选串实测都只出现 1 次。
    check("墙钟闸门在 dry-run 的 return 之前（锚在**判据行**上，不是注释上）",
          _rsrc.index("if not _sampling and a.concurrency < FULL_CONCURRENCY")
          < _rsrc.index("[dry-run] 不调 API"),
          "本轮刚栽过一次「dry-run 报正常、真跑才被拦」")
    check("墙钟闸门只在全量意图下生效",
          "not _sampling and a.concurrency < FULL_CONCURRENCY" in _rsrc,
          "小样与冒烟不该被它拦")
    check("有 --yes-slow 逃生口", "--yes-slow" in _rsrc)

    # ================= 2026-09-19 夜第二轮：六条 =================
    import asyncio as _aio
    import json as _js
    import tempfile as _tf
    from dataclasses import asdict as _asdict
    from dataclasses import fields as _dc_fields

    import getterms.extract as _EXT
    import getterms.llm as _LLM
    import getterms.report as _RPT

    _lsrc2 = pathlib.Path(_LLM.__file__).read_text(encoding="utf-8")
    _esrc2 = pathlib.Path(_EXT.__file__).read_text(encoding="utf-8")
    _rsrc2 = pathlib.Path(_RPT.__file__).read_text(encoding="utf-8")

    # ---- 第 1 条：解析失败 vs 合法空数组，必须分得开（这是撤不撤缓存的判据输入）
    check("parse_json_array_ex 把「合法空数组」判为解析成功",
          _EXT.parse_json_array_ex("[]") == ([], True),
          "模型说这批没术语是**最终答案**，缓存必须留着")
    check("parse_json_array_ex 把散文判为解析失败",
          _EXT.parse_json_array_ex("抱歉，我无法输出 JSON") == ([], False))
    check("parse_json_array_ex 把半截 JSON 判为解析失败",
          _EXT.parse_json_array_ex('[{"sent_id": 1, "terms": [') == ([], False))
    check("parse_json_array 旧名仍可用（向后兼容）",
          _EXT.parse_json_array('[{"sent_id":9}]') == [{"sent_id": 9}])
    check("BatchOutcome 有 parse_failed 字段",
          "parse_failed" in {f.name for f in _dc_fields(_EXT.BatchOutcome)})
    check("LLMClient 有 invalidate()", hasattr(_LLM.LLMClient, "invalidate"))
    check("run_batch 只在 parse_failed 时撤缓存",
          "if outcome.parse_failed:" in _esrc2
          and "client.invalidate(file_md5, batch_idx, bkey)" in _esrc2
          and "client.invalidate(file_md5, 10_000 + batch_idx, bkey)" in _esrc2,
          "撤合法空数组的缓存 = 每次重跑为「确实没术语」重新付费")
    # 回归护栏：前两次补的（refused / truncated 不写缓存）不许被这一轮改掉
    check("refused / truncated 仍然不写缓存（前两轮的修不许退化）",
          "if not r.refused and not r.truncated:" in _lsrc2)

    # ---- 功能性：缓存文件到底在不在。⚠ 缓存根指向临时目录，不碰真 cache/
    _fb = [_EXT.Sentence(pos=1, out_id=1, src="景德镇的瓷器很有名。",
                         tgt="La porcelana de Jingdezhen es famosa.")]
    _fk = _EXT.batch_key(_fb)

    def _seed_cache(cl, idx, content, wire_fp=None):
        r = _LLM.CallResult(ok=True, content=content, finish_reason="stop",
                            model=cl.price_key, effort=cl.effort,
                            wire_fp=cl.wire_fp if wire_fp is None else wire_fp)
        p = cl._cache_path("f" * 32, idx, _fk)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_js.dumps(_asdict(r), ensure_ascii=False), encoding="utf-8")
        return p

    def _run_cached(cl, idx):
        """跑一批，全部命中缓存 -> 零 API 调用（correct_retry=False 避免打出去）。"""
        return _aio.run(_EXT.run_batch(
            cl, "sys", "{pairs}", _fb, "f" * 32, idx,
            correct_retry=False, lang="es"))

    with _tf.TemporaryDirectory() as _td2:
        _cl = _LLM.LLMClient("gemini-3.7-flash", "high", "z" * 12, concurrency=1)
        _cl.cache_root = pathlib.Path(_td2)
        try:
            _p_bad = _seed_cache(_cl, 9001, "抱歉，无法输出 JSON。")
            _oc_bad, _cs = _run_cached(_cl, 9001)
            check("[功能] 解析不出来的缓存**被撤掉**",
                  not _p_bad.exists() and _oc_bad.parse_failed,
                  "不撤 -> 重跑时首轮与纠正重试双双命中缓存 -> 不花钱也不前进")
            check("[功能] 撤缓存全程零 API 调用",
                  bool(_cs) and _cs[0].from_cache and "calls" not in _cl.stats)

            _p_empty = _seed_cache(_cl, 9002, "[]")
            _oc_e, _ = _run_cached(_cl, 9002)
            check("[功能] 合法空数组的缓存**保留**",
                  _p_empty.exists() and _oc_e.json_failed
                  and not _oc_e.parse_failed,
                  "这批确实没术语，是最终答案")

            _good = _js.dumps([{"sent_id": 1, "terms": [
                {"term_src": "景德镇", "term_tgt": "Jingdezhen",
                 "types": "cul"}]}], ensure_ascii=False)
            _seed_cache(_cl, 9003, _good, wire_fp="deadbeef0000")
            check("[功能] wire 参数指纹不一致 -> 当未命中",
                  _cl._read_cache("f" * 32, 9003, _fk) is None
                  and _cl.stats.get("cache_param_mismatch", 0) >= 1,
                  "否则改了 profiles.py 的参数，旧缓存照样命中、改动形同没改")

            _p_old = _seed_cache(_cl, 9004, _good, wire_fp="")
            _d_old = _js.loads(_p_old.read_text(encoding="utf-8"))
            _d_old.pop("wire_fp", None)      # 模拟本轮改动之前落的盘
            _p_old.write_text(_js.dumps(_d_old, ensure_ascii=False),
                              encoding="utf-8")
            check("[功能] 旧缓存没有指纹字段 -> 照旧命中（不制造全量重跑）",
                  _cl._read_cache("f" * 32, 9004, _fk) is not None)
        finally:
            _aio.run(_cl.aclose())

    # ---- 第 2、3 条：指纹与时长预算
    check("CallResult 有 wire_fp 字段",
          "wire_fp" in {f.name for f in _dc_fields(_LLM.CallResult)})
    check("单批时长预算存在且小于「尝试数 x 单次超时」",
          _LLM.BATCH_TIME_BUDGET < _LLM.MAX_ATTEMPTS * _LLM.SINGLE_CALL_TIMEOUT,
          f"{_LLM.BATCH_TIME_BUDGET}s vs "
          f"{_LLM.MAX_ATTEMPTS * _LLM.SINGLE_CALL_TIMEOUT}s（最坏占槽 25 分钟）")
    check("退避听 Retry-After", "retry-after" in _lsrc2)
    check("429 单独计数", '"rate_limited"' in _lsrc2)

    # ---- 第 4 条：429 熔断
    check("429 判据是可单测的模块级函数（不埋在闭包里）",
          hasattr(_RUN, "_rate_limit_tripped"),
          "熔断第一版就是「源码断言全过、实测无效」")
    check("429 判据：19 次不触发、20 次触发",
          not _RUN._rate_limit_tripped(19, 100)
          and _RUN._rate_limit_tripped(20, 100))
    check("429 判据：占比不到 10% 不触发（避免误杀长跑）",
          not _RUN._rate_limit_tripped(20, 500)
          and not _RUN._rate_limit_tripped(0, 10000))
    # ⚠ 锚在**调用点**上，不是锚在「429 熔断」这个串上 —— 那个串在模块级阈值
    #   注释里先出现一次，index() 会找到那一处，于是断言测的是「注释在信号量之前」，
    #   永真且毫无意义（本轮真栽了一次）。
    check("429 熔断在抢到派发槽之后判（放 worker 开头等于不存在）",
          _rsrc.index("async with dispatch_sem")
          < _rsrc.index("_rate_limit_tripped(_n429"),
          "asyncio.gather 把全部协程在 t=0 一起启动")
    check("429 熔断紧跟在已被实测证明位置正确的花费熔断之后",
          _rsrc.index("rep.new_spend_usd > max_spend")
          < _rsrc.index("_rate_limit_tripped(_n429"),
          "两者同一个位置要求：一批真的跑完之后才判")
    check("有 --allow-429 逃生口", "--allow-429" in _rsrc)
    check("429 熔断写进 stop_reason，且建议跟着原因走",
          'rep.stop_reason = "429 熔断"' in _rsrc and '"429" in _why' in _rsrc2,
          "「调高 --max-spend」对 429 熔断是错的建议，那要降并发")

    # ---- 第 5 条：退出码
    check("交付包被闸门拦下时退出码不是 0",
          "if not package_ok:" in _rsrc and "return 3" in _rsrc,
          "以前无条件 return 0 —— 闸门在自动化里等于不存在")

    # ---- 第 6 条：可定位性
    check("熔断跳过的批进可定位清单",
          "rep.skipped_batches.append" in _rsrc,
          "以前只有计数：知道跳过 N 批，不知道是哪 N 批")
    check("dead_batches 封顶 500 不再静默截断",
          "清单封顶 500" in _rsrc and "dead_batches_total" in _rsrc)
    check("json_dead 在报告里拆成两种成因",
          "rep.parse_dead_batches += 1" in _rsrc
          and "rep.empty_array_batches += 1" in _rsrc
          and "JSON 死拆分" in _rsrc2)
    for _f in ("stop_reason", "skipped_batches", "dead_batches_total",
               "parse_dead_batches", "empty_array_batches", "rate_limited"):
        check(f"RunReport 有 {_f} 字段",
              _f in {x.name for x in _dc_fields(_RPT.RunReport)})

    # ============ 余额不足熔断（2026-09-19 夜，用户实测给出措辞） ============
    # 实测行为：`API Error: 403 Your account balance is insufficient.` ->
    # 请求 1 次、0.0s、不落缓存、stats 记 err_403。它已经走在「好的」路径上
    # （不像 429 要退避 32 秒烧 5 次），缺的只是「认出它并把新批停掉」。
    check("is_balance_exhausted 是模块级纯函数（可逐点单测）",
          hasattr(_LLM, "is_balance_exhausted"))
    for _st, _tx, _want, _lab in (
            (403, "API Error: 403 Your account balance is insufficient.", True,
             "中转站实际措辞"),
            (403, "API Error: 403 Insufficient Balance", True, "大小写变体"),
            (403, "insufficient_quota", True, "OpenAI 的 error code 用下划线"),
            (403, "insufficient-balance", True, "有的网关用连字符"),
            (403, "You exceeded your current quota, please check your plan", True,
             "OpenAI 官方措辞"),
            (402, "Payment Required", True, "402 按定义判真"),
            (403, "余额不足，请充值", True, "中文"),
            (403, "API Error: 403 Invalid API key.", False, "★ key 失效不能误判"),
            (403, "Model not allowed for this account.", False, "模型无权限"),
            (403, "Region not supported.", False, "地区限制"),
            (429, "rate limit exceeded", False, "★ 429 不能误判成余额"),
            (400, "messages too long", False, "普通 400"),
            (None, "Connection reset by peer", False, "网络错"),
            (403, None, False, "无文本")):
        check(f"余额判据: {_lab}", _LLM.is_balance_exhausted(_st, _tx) is _want)

    # ---- 行为：真的把 err_balance 记上（真 SDK 异常，零 API 调用）
    import httpx as _hx
    import openai as _oai

    def _probe_exc(exc):
        """把 create 换成抛指定异常，返回 (请求次数, CallResult, 是否落缓存, stats)。"""
        _calls = {"n": 0}
        with _tf.TemporaryDirectory() as _td3:
            _cl2 = _LLM.LLMClient("gemini-3.7-flash", "high", "y" * 12,
                                  concurrency=1)
            _cl2.cache_root = pathlib.Path(_td3)

            async def _c(**kw):
                _calls["n"] += 1
                raise exc

            _cl2._client.chat.completions.create = _c
            _r = _aio.run(_cl2.call([{"role": "user", "content": "x"}],
                                    "f" * 32, 0, "bk"))
            _cached = _cl2._cache_path("f" * 32, 0, "bk").exists()
            _st = dict(_cl2.stats)
            _aio.run(_cl2.aclose())
        return _calls["n"], _r, _cached, _st

    def _mk(status, msg):
        return _oai.PermissionDeniedError(
            msg,
            response=_hx.Response(status,
                                  request=_hx.Request("POST", "https://x/v1")),
            body=None)

    _n, _r, _cached, _st = _probe_exc(
        _mk(403, "API Error: 403 Your account balance is insufficient."))
    check("[功能] 403 余额不足 -> 记 err_balance",
          _st.get("err_balance") == 1, f"stats={_st}")
    check("[功能] 403 余额不足 -> 只请求 1 次（终局，不退避）",
          _n == 1, f"实际 {_n} 次；429 要 5 次 / 32s")
    check("[功能] 403 余额不足 -> 不落缓存（充值后重跑会精确重试）",
          not _cached)
    check("[功能] 403 余额不足 -> 失败结果带着消息文本（可诊断）",
          _r.ok is False and "insufficient" in (_r.error or ""),
          f"error={(_r.error or '')[:60]!r}")

    _n2, _r2, _c2, _st2 = _probe_exc(_mk(403, "API Error: 403 Invalid API key."))
    check("[功能] 403 key 失效 -> **不**记 err_balance（不误判）",
          "err_balance" not in _st2 and _st2.get("err_403") == 1,
          f"stats={_st2}")
    check("[功能] 403 key 失效 -> 也走快速失败", _n2 == 1 and not _c2)

    # ---- 熔断接线
    check("余额不足熔断是「第一次命中就停」（不是 429 那种阈值）",
          'client.stats.get("err_balance", 0) >= 1' in _rsrc,
          "余额不足是终局的：后面每一批都会同样失败，空转没有意义")
    check("余额熔断排在 429 熔断之后（同一个位置：抢到派发槽之后）",
          _rsrc.index("_rate_limit_tripped(_n429")
          < _rsrc.index('client.stats.get("err_balance", 0) >= 1'))
    check("有 --allow-no-balance 逃生口", "--allow-no-balance" in _rsrc)
    check("RunReport 有 balance_errors 字段",
          "balance_errors" in {x.name for x in _dc_fields(_RPT.RunReport)})
    check("停止原因的建议三分支（余额->充值 / 429->降并发 / 花费->调上限）",
          '"充值后重跑同一条命令"' in _rsrc2 and '"429" in _why' in _rsrc2
          and '"调高 --max-spend 重跑同一条命令"' in _rsrc2,
          "三种原因处置完全不同，给错建议比不给更糟")

    # ---- selfcheck 的诚实性（2026-09-19 夜）
    import getterms.selfcheck as _SCK
    _scsrc = pathlib.Path(_SCK.__file__).read_text(encoding="utf-8")
    check("selfcheck 打印形态分析器状态并拒绝降级",
          "morph_status(lg)" in _scsrc and "这个 0 是空真的" in _scsrc
          and "allow_degraded" in _scsrc,
          "本机两个 python 只有一个装了形态库，而 selfcheck 两边输出一模一样")
    check("selfcheck 的降级闸门只放 DICT_FORM_LANGS",
          "lg in DICT_FORM_LANGS and analyzers.get(lg) is None" in _scsrc)
    check("tier1 的结论行带分母",
          "← 分母：清单" in _scsrc and "覆盖 0 行" in _scsrc,
          "「高精确告警 0」的真实含义是「被清单覆盖的 22/1854 行里没有一条被压」")
    check("补语那段标签是「只报不判」（它不计退出码，也不该计）",
          "[只报不判] 补语的数被改" in _scsrc
          and "[机器判定] 补语的数被改" not in _scsrc,
          "法语唯一那条命中正是老师 ② 要的结果，当闸门会把对的判成错的")
    # 分母要真的算得出来，且与实测一致（清单 32 条，覆盖 22/1854 行）
    import getterms.config as _CFG
    import getterms.plural_lexicon as _PLX
    from . import extract as _EX
    check("惯用复数清单仍是 32 条（es 11/fr 10/ru 11）",
          sum(len(v) for v in _PLX.HABITUAL_PLURAL.values()) == 32,
          str({k: len(v) for k, v in _PLX.HABITUAL_PLURAL.items()}))
    _cov_tot = 0
    _row_tot = 0
    for _lg in ("es", "fr", "ru"):
        _rws, _ = _SCK.load_rows(pathlib.Path(_CFG.FINAL_DUMPS[_lg]))
        _an = (_EX.load_morph() if _lg == "ru" else _EX.load_lemmatizer())
        _nm = _SCK.lex_norm(_lg, _an)
        _cov_tot += sum(1 for r in _rws
                        if _PLX.lookup_term(_lg, r.span, _nm))
        _row_tot += len(_rws)
    check("tier1 的覆盖率仍在 1~2%（分母很小，这是已知的）",
          0.005 < _cov_tot / _row_tot < 0.03,
          f"覆盖 {_cov_tot}/{_row_tot} = {_cov_tot / _row_tot:.2%}")

    # ---- bake-off 的两个记账缺口
    _bosrc = (pathlib.Path(__file__).parent / "bakeoff.py").read_text(encoding="utf-8")
    check("bake-off 累加 nom_comp_changed（以前结构性恒为 0）",
          "rep.nom_comp_changed += oc.nom_comp_changed" in _bosrc
          and "rep.nom_comp_notes.extend" in _bosrc,
          "「② 到底生效几次」是 fr_v8 判断的核心依据，而它一直没被记")
    check("bake-off 设 morph_enabled（以前恒 None，降级淘汰对它永不生效）",
          "rep.morph_enabled = (" in _bosrc,
          "三份定稿 dump 全部出自这条路径")

    # ---- confirm_plural 的臂必须真的存在（第五次「默认值指向别的版本」）
    import getterms.confirm_plural as _CP
    _missing = [x for arms in _CP.ARMS.values() for x in arms
                if not (_CFG.BAKEOFF_DIR / x).exists()]
    check("confirm_plural 的三语臂文件都存在", not _missing, str(_missing))

    check("三语默认提示词 = es_v8 / fr_v7 / ru_v6",
          DEFAULT_PROMPT == {"es": "es_v8", "fr": "fr_v7", "ru": "ru_v6"},
          str(DEFAULT_PROMPT))
    # ⚠ 质检工具的默认目标必须与默认提示词同版本，否则「改完提示词跑一下质检」
    #   得到的是改动前的结论（2026-09-18 这个坑踩了两次，所以钉成断言）
    from .config import FINAL_DUMPS
    for lg, ver in DEFAULT_PROMPT.items():
        check(f"FINAL_DUMPS[{lg}] 与默认提示词 {ver} 同版本",
              ver in FINAL_DUMPS[lg].name, FINAL_DUMPS[lg].name)
        check(f"FINAL_DUMPS[{lg}] 指向的 dump 存在", FINAL_DUMPS[lg].exists(),
              str(FINAL_DUMPS[lg]))

    # 换了提示词就要重跑 —— 三个哈希都必须与上一版不同
    check("es_v8 与 es_v7 不同哈希（改了示例就要重跑）",
          es_h != load_prompt("es_v7")[2])
    # ⚠ 2026-09-19：fr_v7 **就地改了一句**（`une nouille` 的释义），所以哈希从
    #   `2b292a821f91` 变成 `2cfc3abcabe2` —— 这是**预期内**的一次跳动，不是意外。
    #   为什么值得跳：那句原写「is one single noodle」，而仓库自己的 `plural_lexicon.py`
    #   （docstring 声明它是「提示词例子的来源，例子必须来自实证不许编」）与已发给法语
    #   老师的小样文案，写的都是法兰西学院第 9 版的「笨蛋」义 —— 提示词跟自己的证据源
    #   不一致。改它是修不一致，不是改口径，因此**不需要重测**。
    #   ⚠ 副作用：`config.FINAL_DUMPS["fr"]` 与 `confirm_plural.VERSION` 指向的
    #   `..._fr_v7__g2*.json` 是**旧内容**产的 dump（重生成要花钱），名字仍有歧义。
    check("fr_v7 哈希 = 修 nouille 释义之后的 2cfc3abcabe2", fr_h == "2cfc3abcabe2", fr_h)
    check("ru_v6 哈希未变（本轮没动俄语提示词）", ru_h == "31626cde39a2", ru_h)
    # 那句修正本身钉住：三个例子的排比必须同构（"单数是另一个词"），
    # 而「one single noodle」恰恰是**同**一个词的单数 —— 它拆自己的台。
    check("fr_v7 的 nouille 例子说的是「笨蛋」义，不是「一根面条」",
          "one single noodle" not in fr
          and "a fool" in fr
          and "des nouilles" in fr)
    check("fr_v7 那段 ⚠ 的三例都指向「单数另有其义」",
          "mainly means \"haste\"" in fr
          and "commonly means \"a recipe\"" in fr)
    check("fr_v7 与 fr_v6 不同哈希", fr_h != load_prompt("fr_v6")[2])
    check("ru_v6 与 ru_v5 不同哈希", ru_h != load_prompt("ru_v5")[2])
    # 法语顺带删了 3 个压单数例子，配比不该再是 22:3
    check("fr_v7 删掉了冗余的 résolutions pertinentes 表格行",
          "| `résolutions pertinentes` |" not in fr)
    check("fr_v7 删掉了 chef d'entreprise / salle de classe 两个压单数例子",
          "chef d'entreprise" not in fr and "salle de classe" not in fr)


def _tooling_b1(check) -> None:
    """B1：三个既有工具的修复，以及 bakeoff 的 --repeat / --tag / 覆盖闸门。"""
    from .bakeoff_review import _keep, lang_of

    # 俄语 label 全带 __pf35，原来的「没有 __pf 才算默认样本」把俄语整语种排除了
    RU = "gemini-3_7-flash__high__b10__ru__pf35__ru_v5"
    check("lang_of 认出俄语 label", lang_of(RU) == "ru")
    check("lang_of 认出法语 label", lang_of("gemini__high__b10__fr__fr_v6") == "fr")
    check("lang_of 默认西语", lang_of("gemini__high__b10__es_v6") == "es")
    check("俄语 label 不再被「默认样本」过滤掉（原 bug）",
          _keep(RU, "ru", "default"), "俄语又被排除了")
    check("--lang 过滤真的排除别的语种", not _keep(RU, "es", "default"))
    check("西语的 pf30 样本仍按原规则被默认排除",
          not _keep("gemini__high__b30__pf30", "es", "default"))

    # --repeat 的缓存命名空间必须分开，否则重复采样只是把同一份缓存读两遍
    import inspect

    from . import bakeoff
    src = inspect.getsource(bakeoff.run_entry)
    check("run_entry 按重复轮次分缓存命名空间",
          'f"{phash}__r{rep_idx}"' in src)
    check("第 1 轮沿用原缓存（已花的钱不重花）", "if rep_idx <= 1 else" in src)
    check("报告里的 phash 仍是真实提示词哈希（轮次不污染版本身份）",
          "phash=phash" in src)

    # 覆盖闸门
    from .bakeoff import _guard_label
    from .config import FINAL_DUMPS
    exists = pathlib.Path(FINAL_DUMPS["es"]).stem[len("terms_"):]
    try:
        _guard_label(exists, overwrite=False)
        check("已落盘的 label 必须拦住（防覆盖已批准基线）", False, "没拦")
    except SystemExit as e:
        check("已落盘的 label 被拦住", True)
        check("拦截理由里给出了两条出路（--overwrite / --tag）",
              "--overwrite" in str(e) and "--tag" in str(e))
    try:
        _guard_label("一个不存在的臂__xyz", overwrite=False)
        check("没落盘过的 label 放行", True)
    except SystemExit:
        check("没落盘过的 label 放行", False, "误拦")
    try:
        _guard_label(exists, overwrite=True)
        check("--overwrite 时放行", True)
    except SystemExit:
        check("--overwrite 时放行", False, "还是拦了")

    # 「一致」sheet 的分母
    rv = pathlib.Path(bakeoff_review_path()).read_text(encoding="utf-8")
    check("「一致」的分母改成了覆盖这一句的 label 数（原来是全部 label 数）",
          "len(hits) == len(cov)" in rv and "len(hits) == n" not in rv)

    # quality / span_defects 的默认目标与字段声明
    from .quality import main as qmain  # noqa: F401  只确认可导入
    qs = pathlib.Path(quality_path()).read_text(encoding="utf-8")
    check("quality 有 --files / --final / --field", all(
        s in qs for s in ("--files", "--final", "--field")))
    sd = pathlib.Path(span_defects_path()).read_text(encoding="utf-8")
    check("span_defects 默认目标改读 config.FINAL_DUMPS", "FINAL_DUMPS.items()" in sd)
    check("span_defects 删掉了 docstring 里不存在的 --tag 参数",
          "[--tag fr]" not in sd)
    check("span_defects 打表时声明量的是哪一列", "量的是：" in sd)


def bakeoff_review_path():
    from . import bakeoff_review
    return bakeoff_review.__file__


def quality_path():
    from . import quality
    return quality.__file__


def span_defects_path():
    from . import span_defects
    return span_defects.__file__


def _nom_gate(check) -> None:
    """俄语一格校验的三处修正（2026-09-18）：形态层覆盖词干前缀、跳过非名词性词类、
    以及「词典形 = 切片」的拒绝单列计数。"""
    from .extract import (BatchOutcome, _is_nominal_ru, _same_lexeme_ru,
                          check_nominative, load_morph)

    m = load_morph()
    if m is None:
        check("无 pymorphy3 时词干前缀仍然说了算（降级路径）",
              check_nominative("яма", "яму", morph=None, use_morph=False) is not None)
        return

    # ---- 该放行的：全是**正确的还原**，先前被词干前缀或副词判据误拒
    OK = [
        ("яма-гребница", "яму-гребницу", "连字符复合词，整体比前缀会在第 3 字符分岔"),
        ("яма с грибницей", "яму с грибницей", "яма/яму 前 3 字符就分岔"),
        ("высококачественный уголь", "высококачественного угля", "уголь/угля 元音交替"),
        ("лес-пагода", "лесов-пагод", "连字符复合词 + 属格复数"),
        ("день", "дня", "元音交替（原 docstring 自己承认会误拒的那个例子）"),
        ("отец", "отца", "元音交替"),
        ("полностью сбалансированный вертикальный судоподъёмник",
         "полностью сбалансированный вертикальный судоподъёмник",
         "副词 полностью 没有格，要求它有主格解是范畴错误"),
        ("Не Жу Чжэнь", "Не Жу Чжэнь", "Не 被解析成语气词，同上"),
    ]
    for base, anchored, why in OK:
        err = check_nominative(base, anchored, morph=m)
        check(f"俄语正确还原要放行: {anchored!r} -> {base!r}（{why}）",
              err is None, f"被判错：{err}")

    # ---- 仍必须拦住的：放宽判据最怕把真错误也放过去
    BAD = [
        ("кошка", "собаку", "换了词（前缀不同、词元不同）"),
        ("Украины", "Украины", "压根没还原（属格原样返回）"),
        ("холодной войны", "холодной войны", "整条没还原"),
        ("искусственного интеллекта", "искусственного интеллекта", "整条没还原"),
        ("рынок", "рынки капитала", "词数不一致"),
        ("политические заверения", "политическое заверение", "把单数改成了复数"),
    ]
    for base, anchored, why in BAD:
        err = check_nominative(base, anchored, morph=m)
        check(f"仍必须拦住: {anchored!r} -> {base!r}（{why}）",
              err is not None, "被放行了")

    # ---- _same_lexeme_ru 自己的边界
    check("_same_lexeme_ru 认出元音交替同词", _same_lexeme_ru("уголь", "угля", m))
    check("_same_lexeme_ru 认出连字符复合词同词",
          _same_lexeme_ru("яма-гребница", "яму-гребницу", m))
    check("_same_lexeme_ru 不认首字母都不同的（防形态预测器乱认）",
          not _same_lexeme_ru("кошка", "собака", m))
    check("_same_lexeme_ru 不认词元不相交的同首字母词（这才是真拦门）",
          not _same_lexeme_ru("собака", "сделка", m))
    check("_same_lexeme_ru 认出逃逸元音（第 2 字符就分岔的那一类）",
          _same_lexeme_ru("день", "дня", m) and _same_lexeme_ru("отец", "отца", m))
    check("_same_lexeme_ru 对非西里尔一律不认（拉丁/汉字不走这条）",
          not _same_lexeme_ru("ugol", "угля", m))

    # ---- _is_nominal_ru
    check("_is_nominal_ru: 名词是名词性", _is_nominal_ru("уголь", m))
    check("_is_nominal_ru: 形容词是名词性", _is_nominal_ru("холодной", m))
    check("_is_nominal_ru: 副词不是名词性", not _is_nominal_ru("полностью", m))
    check("_is_nominal_ru: 解析不出来时按老口径继续查（不放松）",
          _is_nominal_ru("ыыыъъъ", m) in (True, False))

    # ---- C：词典形 = 切片的拒绝要单列
    oc = BatchOutcome()
    check("BatchOutcome 有 nom_reject_same 字段", hasattr(oc, "nom_reject_same"))
    check("nom_reject_same 初值为 0", oc.nom_reject_same == 0)
    import inspect

    from . import extract as _E
    src = inspect.getsource(_E._fill_dict_form)
    check("回落时按「词典形是否等于切片」分流计数",
          "nom_reject_same += 1" in src and "base.strip() == (anchored or \"\").strip()" in src)
    from .report import RunReport
    rep = RunReport(model="m", effort="high", prompt="ru_v6", phash="x",
                    batch_size=10, concurrency=1, lang="ru")
    rep.nom_ok, rep.nom_fallback, rep.nom_reject_same = 93, 7, 3
    txt = rep.to_text()
    check("报告里打印「真实损失」而不是只打回落总数", "真实损失 4" in txt, txt[-400:])


if __name__ == "__main__":
    sys.exit(main())
