"""断言测试：语料异常处理、校验、导出与熔断等验证项，逐个造断言。

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
    读输出的人**分不出「测过了」和「没测」**。跳过必须**当场打印**、并**单独计数**。
    """
    SKIP.append(what)
    print(f"  [跳过] {what} —— {why}")


def main() -> int:
    utf8_stdout()
    # ⚠ 本套断言**从头到尾不调一次 API**，
    #   但有个缓存探针要构造 `LLMClient`，而它的 `__init__` 会读一次 key。
    #   给个哑值，让仓库**克隆下来就能跑**，不必先配一把真 key。
    #   （`load_api_key` 里环境变量优先于 key 文件，所以真 key 不会被碰到。）
    os.environ.setdefault("API_KEY", "sk-dummy-tests-never-call-the-api")

    # ============================================ 纯函数层（不依赖语料）

    # 文件名规则（与既有命名规范逐字一致，不许漂移）
    check("文件名 _align.qc.json",
          replace_out_name("a_align.qc.json") == "a_term.xlsx")
    check("文件名 .align.qc.json（点号）",
          replace_out_name("a.align.qc.json") == "a.term.xlsx")
    check("文件名 .json", replace_out_name("a.json") == "a_term.xlsx")
    check("文件名 .jsonl", replace_out_name("a.jsonl") == "a_term.xlsx")
    check("文件名 其他扩展名", replace_out_name("a.txt") == "a.txt_term.xlsx")

    # 文本归一化
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

    # 类型规范化：num 不在抽取范围，必须被过滤掉
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
    cf = parse_file(sub, "subdir/x_align.qc.json", "z.zip")
    check("异种 schema subtitles 能读出句子",
          cf.schema == "subtitles" and len(cf.sentences) == 2
          and cf.sentences[0].src == "中文一")
    ad = _json.dumps({"project_info": {}, "alignment_data": [
        {"sen_id": 1, "src_time": "0", "src_text": "中文", "tgt_text": "es"}]},
        ensure_ascii=False).encode()
    cf2 = parse_file(ad, "subdir/y_align.qc.json", "z.zip")
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
    check("sent_id 重复 -> 重编（否则术语会挂错句）",
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
        # ⚠ **冻结的字面量**，不是拿 DEFAULT_EXPORT.columns 跟自己比——
        # 那样改列配置照样绿，等于没测。规范必须是外部常量。
        # 第 6 列名固定为 `types`。
        SPEC_COLUMNS = ["sent_id", "src_text", "tgt_text", "term_src", "term_tgt",
                        "types", "note", "term_src_dict", "term_tgt_dict"]
        check("交付表 9 列、列序与规范一致",
              hdr == SPEC_COLUMNS, f"实际 {hdr}")
        check("导出配置与规范同源（配置不许偷偷跑偏）",
              list(DEFAULT_EXPORT.columns) == SPEC_COLUMNS,
              f"配置 {list(DEFAULT_EXPORT.columns)}")
        # ⚠ 按**列名**取，不硬编码下标：在 note 之前插两列时硬编码下标
        #   会读到别的列，而且**很可能仍然是空、照样绿** —— 静默读错列是最坏的一种。
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
    # ⚠ 一个写错的字面花括号（`{…}` 而不是 `{{…}}`）会让 `.format()` 抛 KeyError，
    #   而这两个模板的触发点在导出段的**最末尾**（xlsx 写完之后）：
    #   退出码 1、没有 zip、没有 README、没有报告。这条断言走到打包阶段。
    import re as _re
    with tempfile.TemporaryDirectory() as td:
        # 占位符名字从模板里现取，这样以后新增占位符不会让这条误报。
        _kw = {n: "x" for n in _re.findall(r"\{(\w+)\}", README_TEMPLATE)}
        _kw.update(ts="t", model="m", effort="high", lang="es", batch_size=10,
                   prompt="es", phash="deadbeef", columns="a,b,c",
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

    # ---- 纠正重试的采用条件
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
    # ⚠ 西语语料不在就**可见地跳过**这一整块，不当成失败。
    if not corpus_dir("es").exists():
        print("  [跳过] 西语语料层断言 —— 未提供语料")
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

        # ---- 集成级断言：1 次调用 -> 3 份 xlsx
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

    # ====================================== 词典形词表与反例闸门
    _plural_lexicon(check)
    _false_positives(check)
    _prompt_v7(check)
    _nom_gate(check)

    # 独立成 getterms/tests_fr.py，分文件减少合并冲突。
    tests_fr.run(check, files)

    # ============================================ 俄语层
    # 独立成 getterms/tests_ru.py，同上。
    tests_ru.run(check)

    # ============================================ 汇总
    print(f"通过 {len(PASS)}   失败 {len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
    elif not SKIP:
        print("全部通过")
    return 1 if FAIL else 0


def _hardening(check) -> None:
    """加固断言：钉死「只在全量尺度才出事」或「静默出事」的行为，逐条验证。
    """
    import asyncio
    import contextlib
    import io
    import json as _json

    from . import run as R
    from . import verify as V
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
    check("run.py 不用裸 gather（会让整轮成果归零）",
          "await asyncio.gather(*(worker(f, i, b) for f, i, b in tasks))" not in src)

    # ---- 3 形态分析器降级必须被拦，且报告能标出来
    rep = RunReport(model="gemini-3.7-flash", effort="high", prompt="es",
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

    # ---- 5 限流计数必须被打印（否则会被误当成质量问题）
    check("run.py 打印 client.stats", "client.stats" in src and "调用层计数" in src)

    # ---- 6 refused 不写缓存
    lsrc = (Path(R.LLMClient.__module__ and __import__(
        "getterms.llm", fromlist=["x"]).__file__)).read_text(encoding="utf-8")
    # ⚠ 这里用行为断言而不是源码子串 grep —— 子串挡得住「删掉这一行」，
    #   却证明不了语义。
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

    # ---- 6b 三处畸形结构不许静默丢弃（要计数并触发纠正重试）
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
    # ⚠ 没有 terms 键 = 这句没术语，**不算错**。当成失败会白触发一次纠正重试。
    _o = E.validate_batch([{"sent_id": 1}], _b, lang="es")
    check("没有 terms 键 -> 不算错、不触发重试",
          _o.bad_shape == 0 and _o.no_terms_key == 1 and not _o.failures)
    _o = E.validate_batch([{"sent_id": 1, "terms": []}], _b, lang="es")
    check("terms: [] -> 合法的「没术语」，不计任何错",
          _o.bad_shape == 0 and _o.no_terms_key == 0 and not _o.failures)

    # ---- 6c other 与其余九类互斥（提示词自己定的规则）
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

    # ---- 6e 行序必须按句序（不是批完成顺序）
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
    # ⚠ gather 会把全部协程在 t=0 一起启动，放在 worker 开头的检查等于不存在。
    check("run.py 有派发信号量", "dispatch_sem" in src)
    check("熔断检查在 dispatch_sem 之后",
          src.index("async with dispatch_sem") < src.index('if stop_flag["hit"]'))

    # ---- 6h 语料完整性：必须比 sentences_sent（可用句对），不是 sentences_total
    check("run.py 开跑前查语料完整性", "语料完整性" in src)
    check("语料完整性比的是 sentences_sent",
          "abs(rep.sentences_sent - _full)" in src)
    check("FULL_CORPUS 未登记时取 0 并跳过完整性检查",
          "FULL_CORPUS.get(a.lang, 0)" in src)
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
    rep2 = RunReport(model="gemini-3.7-flash", effort="high", prompt="es",
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

    # ---- 13 成本外推
    _rpt_src = pathlib.Path(
        __import__("getterms.report", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    check("未登记全量句数时不外推（报告只印单价）",
          "FULL_CORPUS.get(self.lang, 0)" in _rpt_src
          and "未登记全量句数，不外推" in _rpt_src)
    check("FULL_CORPUS 是 {语种: 句数}，值必须是整数",
          isinstance(FULL_CORPUS, dict)
          and all(isinstance(v, int) for v in FULL_CORPUS.values()),
          f"实际 {FULL_CORPUS}")

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
        check("verify 对空目录返回非零（不许静默全过）", rc != 0, f"实际 {rc}")
        check("verify 明确说出「没有任何 *term.xlsx」",
              "没有任何 *term.xlsx" in buf.getvalue())

    # ---- 9 verify 的结构问题与还原比例分开裁决
    vsrc = (Path(V.__file__)).read_text(encoding="utf-8")
    check("verify 把「既不是原句切片」从结构问题里剔出去单独裁决",
          '"既不是原句切片" not in q' in vsrc)
    check("verify 的 --lang 限定了取值（写错会静默出假问题）",
          'choices=["es", "fr", "ru", "ja"]' in vsrc)
    check("verify 用 terms_raw.json 的 span 查表（不只靠滑窗）",
          "load_span_index" in vsrc and "stats[\"no_raw\"]" in vsrc)
    check("verify 会核对 xlsx 与 raw 是否同源",
          "xlsx 与 raw 不同源" in vsrc)

    # ---- 补语数变化检测器：校验器抓不到的那一类，必须有人看得见
    from .extract import complement_num_changed as CNC
    for base, span, lang, why in [
        ("immeuble de bureau", "immeubles de bureaux", "fr", "bureaux 被压成单数"),
        ("chaîne de montagne", "chaînes de montagnes", "fr", "montagnes 被压成单数"),
        ("edificio de oficina", "edificios de oficinas", "es", "oficinas 被压成单数"),
        ("mercado de capital", "mercados de capitales", "es", "固定复数补语被压单"),
        ("nouvelle forme d'entreprise", "nouvelles formes d'entreprises", "fr",
         "省音补语（也要人看）"),
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
    check("纠正重试回抄上限 20000（长响应不能截丢）",
          "r.content[:20000]" in esrc and "r.content[:8000]" not in esrc)


def _dict_form_romance(check) -> None:
    """西语/法语的词典形校验（`check_lemma_romance`）。纯函数，不花钱。

    判据的由来：
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
         "并列保留首冠词，冠词不参与还原"),
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
    #   否则在没装 simplemma 的环境里这 2 条会报假失败。
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

    # ---- 锚定闸门：词典形本身仍然锚不上原句
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
    # ⚠ 「词元 ≠ 原词」不是复数信号：simplemma 会给动词词元（`estado`->`estar`）
    #   和抹平性（`antigua`->`antiguo`）。
    #   这个指标是给人工判「习惯复数」用的，报错了就是误导，所以钉死。
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
    """es / fr：词典形一节要在，且不能把已有的规则改丢。"""
    from .extract import load_prompt

    es, _u, es_hash = load_prompt("es")
    check("es 声明 dict_form 输出键", "dict_form" in es)
    check("es 引 ISO 10241-1", "ISO 10241-1" in es)
    check("es 引 IATE 手册", "IATE handbook" in es)
    check("es 明写名词的性绝不改",
          "NEVER change the gender of a noun." in es)
    check("es 举了 la política / el político 这个陷阱",
          "político" in es and "policy" in es)
    check("es 明写习惯复数保留",
          "HABITUALLY USED in the plural" in es)
    check("es 举了 derechos humanos", "derechos humanos" in es)
    check("es 明写专名不动",
          "Proper names and abbreviations never change." in es)
    check("es 明写词数不变", "Never change the number of words." in es)
    # es 的已有规则必须还在
    check("es 保留 other 互斥", "`other` is exclusive" in es)
    check("es 保留 tab 叠加", "`tab` is additive" in es)
    check("es 保留 tab 必须照抽", "Sensitive material is in scope" in es)
    check("es 保留最长跨度规则",
          "Prefer the LONGEST span that is still a single term" in es)
    check("es 保留召回优先", "recall matters more than" in es)

    fr, _u2, fr_hash = load_prompt("fr")
    check("fr 声明 dict_form 输出键", "dict_form" in fr)
    check("fr 引 ISO 10241-1", "ISO 10241-1" in fr)
    check("fr 明写名词的性绝不改",
          "NEVER change the gender of a noun." in fr)
    check("fr 明写习惯复数保留",
          "HABITUALLY USED in the plural" in fr)
    check("fr 举了 droits de l'homme", "droits de l'homme" in fr)
    # fr 的已有规则必须还在
    check("fr 保留冠词不入 span 的规则",
          "French articles, prepositions and elisions are NOT part of the term"
          in fr)
    check("fr 保留并列保留首冠词",
          "a coordinated term keeps its leading article" in fr)
    check("fr 保留 tour/scen 口头语不抽的反例",
          "travel vlogs" in fr)
    check("fr 保留 other 互斥", "`other` is exclusive" in fr)

    # ---- fr：三条补语规则
    fr6, _u3, fr6_hash = load_prompt("fr")
    # ⚠ 这里只钉 fr 提示词内容，默认提示词选谁由 _prompt_v7 钉。
    # ② 补语表类别 -> 单数
    check("fr 明写补语取「该术语约定的数」而不是句中的数",
          "takes the number the ESTABLISHED TERM has" in fr6)
    check("fr 用 chef-d'œuvre 说明补语不随中心词配合",
          "chef-d'œuvre" in fr6 and "does not\n  agree with its head" in fr6)
    check("fr 写明 entreprise 取单数",
          "nouvelle forme d'entreprise" in fr6
          and "nouvelle forme d'entreprises" not in fr6)
    # ③ 构成实体 -> 保留复数，这是 ② 的护栏，必须同等醒目
    for ex in ["immeuble de bureaux", "chaîne de montagnes",
               "groupement d'entreprises", "banc de poissons",
               "marché des capitaux"]:
        check(f"fr 举了 ③ 类反例（不许压单数）: {ex}", ex in fr6)
    check("fr 明说把 ③ 类压成单数是错的",
          "Making these singular is a\n    real error" in fr6)
    # ① 机构专名
    check("fr 把机构名写进「永不改动」那一条",
          "institution names" in fr6 and "Organisation des Nations Unies" in fr6)
    # fr 的已有规则必须还在
    check("fr 保留并列保留首冠词",
          "a coordinated term keeps its leading article" in fr6)
    check("fr 保留名词的性绝不改",
          "NEVER change the gender of a noun." in fr6)
    check("fr 保留习惯复数",
          "HABITUALLY USED in the plural" in fr6)
    check("fr 保留 tour/scen 口头语不抽的反例", "travel vlogs" in fr6)
    check("fr 保留 other 互斥", "`other` is exclusive" in fr6)


def _plural_lexicon(check) -> None:
    """惯用复数清单：它是三个提示词「保留复数」例子的唯一来源，得先站得住。"""
    from .plural_lexicon import (HABITUAL_PLURAL, Entry, examples_for_prompt,
                                 heads, lookup, meaning_shift)

    # ⚠ 门槛只是下限：
    #   **一份短而每条有出处的清单，强于一份长而靠类推的清单** —— 条数不是质量指标。
    for lang, n_min in (("es", 10), ("fr", 10), ("ru", 10)):
        ents = HABITUAL_PLURAL[lang]
        check(f"{lang} 惯用复数清单至少 {n_min} 条", len(ents) >= n_min,
              f"实际 {len(ents)}")
        check(f"{lang} 每条都写了出处（日后要能判断该不该信）",
              all(e.source for e in ents))
        check(f"{lang} 每条都写了为什么惯用复数", all(e.why for e in ents))
        check(f"{lang} 每条都有中文对应（要能人工核对）", all(e.zh for e in ents))
        check(f"{lang} 中心词必须出现在完整术语里",
              all(e.head.casefold() in e.term.casefold() for e in ents),
              str([e.head for e in ents if e.head.casefold() not in e.term.casefold()]))
        check(f"{lang} 中心词不重复（重复就说明清单在打自己）",
              len({e.head.casefold() for e in ents}) == len(ents))

    # 这些词必须在清单里（它们会被误压成单数）
    for lang, w in (("es", "palillos"), ("es", "elecciones"),
                    ("fr", "précipitations"), ("fr", "objectifs"),
                    ("fr", "nouilles"), ("ru", "осадки"), ("ru", "рынки")):
        check(f"{lang} 清单收了实证被压错的 {w}", lookup(lang, w) is not None)
    # ⚠ `precipitaciones` 不在西语清单里：IATE 45260 与 AEMET 术语手册都写单数
    #   `precipitación atmosférica`，DLE 气象义也没有 `U. m. en pl.` 标记。
    #   法语那侧**相反**（Larousse 另立复数词条）—— 跨语种类推不可靠的活例子。
    check("es 清单不含 precipitaciones（IATE/AEMET/DLE 三处都指向单数）",
          lookup("es", "precipitaciones") is None)
    check("fr 清单仍收 précipitations（法语确实惯用复数，与西语相反）",
          lookup("fr", "précipitations") is not None)
    # 另两条跨语种类推的产物同样不在清单里：DLE 的 `gasto público` 是单数子词条且不标复数
    # （同页 `gasto social` 标 U. t. en pl.、`gastos de representación` 以 m. pl. 立目，
    #  该标会标）；IATE 1492661 的词条形是 `infraestructura`。
    check("es 清单不含 gastos（DLE 单数子词条、IATE 裸复数 0 命中）",
          lookup("es", "gastos") is None)
    check("es 清单不含 infraestructuras（IATE 1492661 词条形是单数）",
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

    # ② 绝不该命中的 —— 每条都是会被中心词误命中的
    check("lookup_term 不把 sources d'eau 当惯用复数（tier1 假阳性）",
          lookup_term("fr", "sources d'eau") is None)
    check("lookup_term 不靠中心词乱认：masas de trabajadores",
          lookup_term("es", "masas de trabajadores") is None)
    check("lookup_term 不靠共享的 de 乱认：droits de l'enfant",
          lookup_term("fr", "droits de l'enfant") is None)
    check("lookup_term 空切片返回 None", lookup_term("es", "") is None)
    check("lookup_term 未登记语种返回 None", lookup_term("ja", "麺") is None)

    # ③ 不在清单里的两条（查实该用单数，模型交的单数才对）
    check("清单不含 sources de Baotu（趵突泉是一处专名泉，复数形精确检索 0 命中）",
          lookup("fr", "sources") is None
          and all("Baotu" not in e.term for e in HABITUAL_PLURAL["fr"]))
    check("清单不含 vêtements chinois（vêtement 是普通可数名词；汉服的法语标准词是 hanfu）",
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
        check("lookup 对俄语斜格是全盲的（所以 tier1 用 lookup_term）",
              lookup("ru", "осадков") is None)

    # ⑤ 俄语的「压单数即换义」必须非空（不能恒空）
    ms_ru = [e.head for e in meaning_shift("ru")]
    check("ru meaning_shift 有 7 条", len(ms_ru) == 7, str(ms_ru))
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


def _false_positives(check) -> None:
    """**反例断言**：只出假阳性的检测器不许再回来，任何一个被加回来都要红。

    一个喊狼的检测器比没有更糟 —— 它训练人忽略告警。
    这里把「绝不能报」的输入钉成「必须不报」。
    """
    from .extract import (check_nominative, complement_num_changed, head_group,
                          load_morph)

    # ① 俄语中心名词性翻转：pymorphy3 对这些词的性解析不唯一，会误报。
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

    # ③ 并列连词不是介词 —— 把 y/et 当介词会报假阳性
    for base, span, lang in [("porcelaine bleue et blanche",
                              "porcelaines bleues et blanches", "fr"),
                             ("pequeña y microempresa",
                              "pequeñas y microempresas", "es")]:
        check(f"{lang} 并列结构不报「补语的数被改」: {span}",
              complement_num_changed(base, span, lang) is None,
              str(complement_num_changed(base, span, lang)))

    # ④ 术语对错配的长度比启发式：两侧是各自独立锚定的，没有跨语对齐地基
    import ast
    esrc = pathlib.Path(__import__("getterms.extract", fromlist=["x"]).__file__)
    elits = {n.value.strip() for n in ast.walk(ast.parse(esrc.read_text(encoding="utf-8")))
             if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("抽取层不含「长度比判错配」这种无地基的启发式",
          "len_ratio" not in elits and "长度比" not in elits)


def _prompt_v7(check) -> None:
    """es / fr / ru：A1~A3 到位，且已有的口径一条没丢。"""
    from .extract import load_prompt

    es, _u, es_h = load_prompt("es")
    fr, _u2, fr_h = load_prompt("fr")
    ru, _u3, ru_h = load_prompt("ru")

    for name, txt in (("es", es), ("fr", fr), ("ru", ru)):
        # A1：判据是 IATE 原话，且明确否掉「单数存在就能压单数」
        # ⚠ 提示词是折行的，判措辞前先把空白压平，否则断言会挂在换行位置上
        flat0 = " ".join(txt.split())
        check(f"{name} A1 判据写成 HABITUALLY USED in the plural",
              "HABITUALLY USED in the plural" in flat0)
        check(f"{name} A1 明说判据不是「单数是否存在」",
              'The test is NOT "does a singular exist"' in flat0)
        check(f"{name} A1 不含 only exist(s) in the plural 这个窄判据",
              "only exists in the plural" not in txt
              and "only exist in the plural" not in txt)
        # A2：中心词的兜底句
        check(f"{name} A2 写明中心词「拿不准就保留句中形式」",
              "KEEP THE\n  FORM THE SENTENCE USED" in txt
              or "KEEP THE\n  NUMBER THE SENTENCE USED" in txt)
        flat = " ".join(txt.split())    # 提示词是折行的，判措辞时先把空白压平
        check(f"{name} A2 明说这条兜底管中心词不只管补语",
              "HEAD noun, not only to complements" in flat
              or "HEAD word, not only to subordinate nouns" in flat)

    # A3：例子必须来自实证清单，不许编
    from .plural_lexicon import HABITUAL_PLURAL
    for lang, txt, name in (("es", es, "es"), ("fr", fr, "fr"), ("ru", ru, "ru")):
        hit = [e.term for e in HABITUAL_PLURAL[lang] if e.term in txt]
        check(f"{name} A3 至少引了 6 条清单里的实证例子", len(hit) >= 6,
              f"只引了 {len(hit)}: {hit}")
    # ⚠ 断言只能钉住有出处的东西；钉之前先查出处。
    check("es 不出现 precipitaciones（IATE 45260 / AEMET / DLE 都指向单数）",
          "precipitaci" not in es)
    check("es 用 DLE 查实的词做「压单数即换词」的例子",
          '`palillo` means "a toothpick"' in es and '`masa` means "mass"' in es)
    check("es 示例表有 palillos 那行（DLE 义 12 只立复数）",
          "| `palillos` | `palillos` |" in es)
    check("es 的「本领域写作复数」列表里没有 gastos públicos / infraestructuras",
          "gastos públicos" not in es and "infraestructuras" not in es)
    # 这几个词是「跨语种类推」的产物 —— 这条断言是为了不让它们悄悄回来
    check("es 列表里每一项都在清单里有对应条目（例子只许来自实证清单）",
          all(w in es for w in ("elecciones", "recursos hídricos", "ingresos fiscales",
                                "masas populares", "palillos")))
    check("fr 举了 objectifs de développement durable",
          "objectifs de développement durable" in fr)
    check("ru 举了 атмосферные осадки", "атмосферные осадки" in ru)

    # 俄语那段「反向加压」必须挂上例外指针，否则与 A1 自相矛盾
    check("ru 的「复数不自动等于词典形」挂了惯用复数例外",
          "unless the term is one\n  of the habitually-plural ones listed above" in ru)

    # ---- 已有规则一条都不许丢
    for name, txt in (("es", es), ("fr", fr), ("ru", ru)):
        check(f"{name} 保留 other 互斥", "`other` is exclusive" in txt)
        check(f"{name} 保留 tab 必须照抽", "Sensitive material is in scope" in txt)
        check(f"{name} 保留词数不变", "Never change the number of words." in txt)
        check(f"{name} 声明 dict_form 输出键", "dict_form" in txt)
        check(f"{name} 引 ISO 10241-1", "ISO 10241-1" in txt)
    check("es 保留最长跨度规则",
          "Prefer the LONGEST span that is still a single term" in es)
    check("es 保留名词的性绝不改", "NEVER change the gender of a noun." in es)
    check("fr 保留 ② 普通名词定语表类别",
          "racine de lotus" in fr and "aliment à base de farine" in fr)
    for ex in ["immeuble de bureaux", "chaîne de montagnes",
               "groupement d'entreprises", "banc de poissons", "marché des capitaux"]:
        check(f"fr 保留 ③ 类反例: {ex}", ex in fr)
    check("fr 保留并列保留首冠词",
          "a coordinated term keeps its leading article" in fr)
    check("fr 写明 nouvelle forme d'entreprise",
          "nouvelle forme d'entreprise" in fr
          and "nouvelle forme d'entreprises" not in fr)
    check("ru 保留从属名词保留自己的格",
          "A subordinate noun keeps its own case." in ru)
    check("ru 保留一格还原（主格）", "nominative" in ru.lower())

    # 默认提示词必须指向当前的三个版本（改这里等于换口径）
    from .run import DEFAULT_PROMPT

    # ---- 交付前的词典形一致性收口（装配层）
    from .extract import TermRow as _TR
    from .extract import unify_dict_forms as _UDF

    def _row(zh: str, span: str, base: str) -> object:
        return _TR(sent_id=1, src_text="x", tgt_text="y", term_src=zh,
                   term_tgt=span, types="cul", term_tgt_base=base)

    # 例：松茸，同一文件三次，2:1
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

    # ---- 每个 CLI 模块的 --help 都要能出来
    import contextlib as _ctx
    import importlib as _il
    import io as _io
    for _m in ("run", "verify"):
        _buf = _io.StringIO()
        _err = ""
        try:
            _mod = _il.import_module(f".{_m}", package="getterms")
            with _ctx.redirect_stdout(_buf), _ctx.redirect_stderr(_buf):
                try:
                    _mod.main(["--help"])
                except SystemExit as _e:
                    if _e.code not in (0, None):
                        _err = f"exit {_e.code}"
        except Exception as _e:
            _err = f"{type(_e).__name__}: {_e}"[:160]
        check(f"{_m} --help 不崩", not _err, _err)


    # ---- 墙钟与并发常量
    import getterms.run as _RUN
    _rsrc = pathlib.Path(_RUN.__file__).read_text(encoding="utf-8")
    check("墙钟常量取上沿",
          _RUN.SECONDS_PER_BATCH >= 41.6,
          f"常量 {_RUN.SECONDS_PER_BATCH}")
    check("全量并发为 40",
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
    # ⚠ 锚在**可执行的判据行**上，不能锚在注释里的串上 —— 注释搬走后断言照样绿，
    #   而它声称要防的故障照样发生。
    check("墙钟闸门在 dry-run 的 return 之前（锚在**判据行**上，不是注释上）",
          _rsrc.index("if not _sampling and a.concurrency < FULL_CONCURRENCY")
          < _rsrc.index("[dry-run] 不调 API"),
          "闸门必须在 dry-run 提前返回之前生效")
    check("墙钟闸门只在全量意图下生效",
          "not _sampling and a.concurrency < FULL_CONCURRENCY" in _rsrc,
          "抽样与试跑不该被它拦")
    check("有 --yes-slow 逃生口", "--yes-slow" in _rsrc)

    # ================= 缓存与熔断：六条 =================
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
    # 回归护栏：refused / truncated 不写缓存的行为不许被改掉
    check("refused / truncated 仍然不写缓存",
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
            _d_old.pop("wire_fp", None)      # 模拟没有该字段的旧缓存
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
          "判据必须可单测")
    check("429 判据：19 次不触发、20 次触发",
          not _RUN._rate_limit_tripped(19, 100)
          and _RUN._rate_limit_tripped(20, 100))
    check("429 判据：占比不到 10% 不触发（避免误杀长跑）",
          not _RUN._rate_limit_tripped(20, 500)
          and not _RUN._rate_limit_tripped(0, 10000))
    # ⚠ 锚在**调用点**上，不是锚在「429 熔断」这个串上 —— 那个串在模块级阈值
    #   注释里先出现一次，index() 会找到那一处，于是断言测的是「注释在信号量之前」，
    #   永真且毫无意义。
    check("429 熔断在抢到派发槽之后判（放 worker 开头等于不存在）",
          _rsrc.index("async with dispatch_sem")
          < _rsrc.index("_rate_limit_tripped(_n429"),
          "asyncio.gather 把全部协程在 t=0 一起启动")
    check("429 熔断紧跟在花费熔断之后",
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
          "闸门必须反映在退出码里")

    # ---- 第 6 条：可定位性
    check("熔断跳过的批进可定位清单",
          "rep.skipped_batches.append" in _rsrc,
          "要知道跳过的是哪几批，不能只有计数")
    check("dead_batches 封顶 500 时报告总数（不静默截断）",
          "清单封顶 500" in _rsrc and "dead_batches_total" in _rsrc)
    check("json_dead 在报告里拆成两种成因",
          "rep.parse_dead_batches += 1" in _rsrc
          and "rep.empty_array_batches += 1" in _rsrc
          and "JSON 死拆分" in _rsrc2)
    for _f in ("stop_reason", "skipped_batches", "dead_batches_total",
               "parse_dead_batches", "empty_array_batches", "rate_limited"):
        check(f"RunReport 有 {_f} 字段",
              _f in {x.name for x in _dc_fields(_RPT.RunReport)})

    # ============ 余额不足熔断 ============
    # 该错误串对应的行为：403 余额不足 -> 请求 1 次、不落缓存、stats 记 err_403。
    # （不像 429 要退避 32 秒烧 5 次），缺的只是「认出它并把新批停掉」。
    check("is_balance_exhausted 是模块级纯函数（可逐点单测）",
          hasattr(_LLM, "is_balance_exhausted"))
    for _st, _tx, _want, _lab in (
            (403, "API Error: 403 Your account balance is insufficient.", True,
             "常见服务商措辞"),
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

def _nom_gate(check) -> None:
    """俄语一格校验：形态层覆盖词干前缀、跳过非名词性词类、
    以及「词典形 = 切片」的拒绝单列计数。"""
    from .extract import (BatchOutcome, _is_nominal_ru, _same_lexeme_ru,
                          check_nominative, load_morph)

    m = load_morph()
    if m is None:
        check("无 pymorphy3 时词干前缀仍然说了算（降级路径）",
              check_nominative("яма", "яму", morph=None, use_morph=False) is not None)
        return

    # ---- 该放行的：全是**正确的还原**（词干前缀/副词判据容易误拒）
    OK = [
        ("яма-гребница", "яму-гребницу", "连字符复合词，整体比前缀会在第 3 字符分岔"),
        ("яма с грибницей", "яму с грибницей", "яма/яму 前 3 字符就分岔"),
        ("высококачественный уголь", "высококачественного угля", "уголь/угля 元音交替"),
        ("лес-пагода", "лесов-пагод", "连字符复合词 + 属格复数"),
        ("день", "дня", "元音交替"),
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
    check("_is_nominal_ru: 解析不出来时仍继续查（不放松）",
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
    rep = RunReport(model="m", effort="high", prompt="ru", phash="x",
                    batch_size=10, concurrency=1, lang="ru")
    rep.nom_ok, rep.nom_fallback, rep.nom_reject_same = 93, 7, 3
    txt = rep.to_text()
    check("报告里打印「真实损失」而不是只打回落总数", "真实损失 4" in txt, txt[-400:])


if __name__ == "__main__":
    sys.exit(main())
