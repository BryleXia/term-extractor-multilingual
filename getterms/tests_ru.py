"""俄语轮次的断言，单独一个文件。

为什么独立成文件：避免与 `tests.py` 的改动互相冲突。
`tests.py` 只调一次 `tests_ru.run(check)`。

覆盖：
  * 散装 `.json` 载体（`load_corpus` 也要读散装 json，否则静默读出 0 个文件）
  * 西里尔字母在 `is_meaningful` / `cjk_ratio` / `casefold` 上的行为
  * `ё`/`е` 正字法折叠档 —— 命中后必须仍返回**原文切片**
  * 屈折（变格）刻意**不宽容**，这是当前的已知边界，写成断言防止被无意放松
  * 测试语料的规模、分层、方向、零异常
用法：由 `python -m getterms.tests` 自动带上，不单独跑。
"""
from __future__ import annotations

import collections

from .config import corpus_dir
from .corpus import cjk_ratio, is_meaningful, load_corpus, loose_paths
from .extract import find_verbatim


# 固定用例：真实语料里的句子，用作屈折与 ё/е 的锚定样本
S_UKR = "Без изменения внеблокового статуса Украины."
S_REGIME = ("Запад продолжал планомерно осуществлять милитаризацию русофобского "
            "киевского режима, который был приведен к власти в результате "
            "кровавого госпереворота")
S_LIFT = ("судоподъёмник может плавно подниматься и опускаться, словно двигаясь "
          "по ровной поверхности.")


def _pure(check) -> None:
    """纯函数层：不依赖语料，随时可跑。"""
    # ---- 西里尔字母的基本判定
    check("is_meaningful 认西里尔字母", is_meaningful("Украины"))
    check("is_meaningful 仍不认纯数字/标点", not is_meaningful("20%. —"))
    check("cjk_ratio 不把西里尔误判成中文", cjk_ratio(S_REGIME) < 0.05,
          f"实际 {cjk_ratio(S_REGIME):.3f}")
    check("cjk_ratio 认中文", cjk_ratio("恐俄基辅政权军事化") > 0.9)

    # ---- casefold 对俄语等长（find_verbatim 第二档依赖等长才敢回切下标）
    words = [w for w in S_REGIME.replace(",", " ").split() if w]
    check("casefold 对俄语词等长",
          all(len(w.casefold()) == len(w) for w in words),
          f"非等长: {[w for w in words if len(w.casefold()) != len(w)]}")

    # ---- ё/е 折叠档：命中后必须返回**原文切片**，不是模型写的字符串
    got = find_verbatim("все", "Это всё, что есть")
    check("ё/е 折叠命中且返回原文切片 всё", got == "всё", f"实际 {got!r}")
    got = find_verbatim("всё", "Это все, что есть")
    check("ё/е 反向也命中，返回原文切片 все", got == "все", f"实际 {got!r}")
    got = find_verbatim("судоподъемник", S_LIFT)
    check("缺两点的写法能锚到带 ё 的原文", got == "судоподъёмник", f"实际 {got!r}")
    got = find_verbatim("на подъеме", "идём на подъёме сейчас")
    check("多词 + ё/е 混用（走空白折叠档）", got == "на подъёме", f"实际 {got!r}")

    # ---- 变格形式必须能精确锚定
    for term in ("внеблокового статуса", "Украины"):
        check(f"变格形式精确锚定: {term}", find_verbatim(term, S_UKR) == term)
    for term in ("русофобского киевского режима", "кровавого госпереворота"):
        check(f"变格形式精确锚定: {term}", find_verbatim(term, S_REGIME) == term)

    # ---- 词典形**刻意**锚不上：这是当前的已知边界
    #      若哪天真加了词干匹配第四档，这几条会失败 —— 那时必须同步改判据，
    #      而不是悄悄放宽这道反幻觉闸门。
    for lemma in ("внеблоковый статус", "Украина"):
        check(f"词典形刻意锚不上（无形态学宽容）: {lemma}",
              find_verbatim(lemma, S_UKR) is None)
    check("词典形刻意锚不上: русофобский киевский режим",
          find_verbatim("русофобский киевский режим", S_REGIME) is None)

    # ---- 折叠不能把不同的词混为一谈
    check("ё/е 折叠不产生假阳性", find_verbatim("весь", "Это всё, что есть") is None)


def _corpus(check) -> None:
    """真实语料层：俄语**测试**语料（`ru_test`，10 个散装 json）。

    ⚠ `corpus_dir("ru")` 指向全量语料，测试语料在 `ru_test`。
      这一节钉住测试语料的规模与出处；全量的断言在 _corpus_full。
    """
    cdir = corpus_dir("ru_test")
    if not cdir.exists():
        check("俄语语料目录存在", False, f"找不到 {cdir}")
        return

    # ---- 载体：散装 json（只 glob zip 会静默读出 0 个文件）
    check("俄语语料是散装 json，不是 zip",
          len(loose_paths(cdir)) == 10 and not list(cdir.glob("*.zip")),
          f"散装 {len(loose_paths(cdir))} 个，zip {len(list(cdir.glob('*.zip')))} 个")

    files, skipped = load_corpus(cdir)
    check("散装 json 被 load_corpus 读到 10 个", len(files) == 10, f"实际 {len(files)}")
    check("来源名标成散装", {f.zip_name for f in files} == {"(散装)"},
          f"实际 {sorted({f.zip_name for f in files})}")
    check("无跳过条目", len(skipped) == 0, f"实际 {skipped}")

    # ---- 规模
    total = sum(len(f.sentences) for f in files)
    usable = sum(len(f.usable_sentences) for f in files)
    check("总句对 1069", total == 1069, f"实际 {total}")
    check("可用句对 1061", usable == 1061, f"实际 {usable}")
    check("不可用 8 句", total - usable == 8, f"实际 {total - usable}")

    # ---- 分层与方向
    layers = {k: sum(len(f.usable_sentences) for f in files if f.layer() == k)
              for k in {f.layer() for f in files}}
    want = {"tour/serv": 372, "conf/econ": 257, "conf/poli": 254, "conf/tech": 178}
    check("四个层的可用句数", layers == want, f"实际 {layers}")
    dirs = collections.Counter(f.name_direction() for f in files)
    check("方向 zh-ru 5 / ru-zh 5", dirs == {"zh-ru": 5, "ru-zh": 5}, f"实际 {dict(dirs)}")

    # ---- 零异常：这是俄语语料最大的特点，比西/法都干净
    check("无异种 schema", all(f.schema == "standard" for f in files),
          f"实际 {[(f.basename, f.schema) for f in files if f.schema != 'standard']}")
    check("无 sent_id 重编",
          not [f for f in files if any("重编" in n for n in f.notes)])
    check("无 JSON 语法修复", not [f for f in files if any("逗号" in n for n in f.notes)])
    check("无字段错位还原", not [f for f in files if any("错位" in n for n in f.notes)])
    check("无文件名归一化（俄语文件名全部规范）",
          all(f.basename.endswith("_align.qc.json") for f in files),
          f"实际 {[f.basename for f in files if not f.basename.endswith('_align.qc.json')]}")
    check("输出名格式正确",
          all(f.out_name == f.stem_key + "_term.xlsx" for f in files),
          f"实际 {[(f.basename, f.out_name) for f in files][:2]}")
    check("无 md5 重复组", len({f.md5 for f in files}) == 10)

    # ---- 7 句中文侧整句为空（口译当场省略），要被判为不可用
    by = {f.basename: f for f in files}
    f6 = by.get("ru-zh_conf_poli_0006_seg002_align.qc.json")
    if f6 is None:
        check("找到 ru-zh_conf_poli_0006_seg002", False)
    else:
        empty = [s.pos for s in f6.sentences if not s.usable]
        check("conf_poli_0006_seg002 的 6 句中文侧为空被判不可用",
              empty == [3, 23, 35, 48, 53, 82], f"实际 {empty}")
        check("这 6 句俄语侧其实有内容（是漏译不是空行）",
              all(is_meaningful(s.src) for s in f6.sentences if s.pos in empty))

    # ---- 中文侧系统性 ASR 错字：按逐字锚定原样收录（不在本工具里改字）
    ft = by.get("zh-ru_conf_tech_0001_seg005_align.qc.json")
    if ft is None:
        check("找到 zh-ru_conf_tech_0001_seg005", False)
    else:
        bad = sum(s.src.count("生船机") + s.tgt.count("生船机") for s in ft.sentences)
        good = sum(s.src.count("升船机") + s.tgt.count("升船机") for s in ft.sentences)
        check("conf_tech_0001_seg005 的「生船机」错字 7 处", bad == 7, f"实际 {bad}")
        # 这一条更关键：同一个文件里两种写法并存（第 59 句是对的），
        # 术语库会拿到两个条目指向同一概念，而逐字锚定要求两个都如实收录。
        check("同一文件里正确写法「升船机」只有 1 处（两种写法并存）", good == 1,
              f"实际 {good}")


def _corpus_full(check) -> None:
    """真实语料层：俄语**全量**语料（9 个 zip）。

    与测试语料完全不同构：zip 载体、6 个层、533 个文件，
    并且带来了西语法语都没有的第 4 类命名缺陷。
    """
    cdir = corpus_dir("ru")
    if not cdir.exists():
        check("俄语全量语料目录存在", False, f"找不到 {cdir}")
        return

    check("俄语全量是 9 个 zip（不再是散装 json）",
          len(list(cdir.glob("*.zip"))) == 9,
          f"实际 zip {len(list(cdir.glob('*.zip')))} 个")

    files, skipped = load_corpus(cdir)
    check("全量 533 个文件", len(files) == 533, f"实际 {len(files)}")
    check("无跳过条目", len(skipped) == 0, f"实际 {skipped}")

    total = sum(len(f.sentences) for f in files)
    usable = sum(len(f.usable_sentences) for f in files)
    check("可用句对不超过总句对", usable <= total, f"实际 {usable}/{total}")
    from .report import FULL_CORPUS
    check("FULL_CORPUS[ru] 若登记则等于语料可用句对",
          "ru" not in FULL_CORPUS or FULL_CORPUS["ru"] == usable,
          f"实际 {FULL_CORPUS.get('ru')}")

    layers = sorted({f.layer() for f in files})
    check("全量 6 个层，与西语同构（无 tour/scen）",
          layers == ["conf/econ", "conf/poli", "conf/tech",
                     "tour/attr", "tour/muse", "tour/serv"],
          f"实际 {layers}")

    # ---- 命名缺陷：重复下载后缀与游离空格（西/法都没有）
    ren = sorted(f.basename for f in files
                 if any("文件名拼写已纠正" in n for n in f.notes))
    check("恰好 3 个文件名被归一化", len(ren) == 3, f"实际 {ren}")
    check("归一化覆盖「.qc 后游离空格」与「(1) 重复下载后缀」两种形态",
          any(b.endswith("_align.qc .json") for b in ren)
          and sum(1 for b in ren if b.endswith("_align.qc (1).json")) == 2,
          f"实际 {ren}")
    # 这 3 个都没有规范名兄弟 —— 是真内容，不是重复下载，不能丢
    have = {f.basename for f in files}
    for b in ren:
        sib = b.replace("_align.qc .json", "_align.qc.json").replace(
            "_align.qc (1).json", "_align.qc.json")
        check(f"{b[:38]}… 没有规范名兄弟（是真内容不是副本）", sib not in have)
    check("归一化后输出名全部规范",
          all(f.out_name.endswith("_term.xlsx")
              and " " not in f.out_name and "(" not in f.out_name for f in files),
          f"实际 {[f.out_name for f in files if ' ' in f.out_name or '(' in f.out_name]}")

    # ---- 输出名零碰撞：run.py 开跑前会硬校验这一条
    names = [f.out_name for f in files]
    check("俄语全量输出名零碰撞", len(set(names)) == len(names),
          f"唯一 {len(set(names))} / 共 {len(names)}")

    # ---- 全量比测试语料干净的地方
    check("无异种 schema", all(f.schema == "standard" for f in files),
          f"实际 {[(f.basename, f.schema) for f in files if f.schema != 'standard']}")
    check("无字段错位还原", not [f for f in files if f.shifted_recovered])
    check("无 md5 重复组", len({f.md5 for f in files}) == 533)
    dirs = collections.Counter(f.name_direction() for f in files)
    check("方向 zh-ru 419 / ru-zh 114",
          dirs == {"zh-ru": 419, "ru-zh": 114}, f"实际 {dict(dirs)}")


def _prompt(check) -> None:
    """ru 提示词：语言专属的两条规则必须在，且示例要覆盖全部 10 类。"""
    from .extract import load_prompt

    try:
        system, _, _ = load_prompt("ru")
    except FileNotFoundError as e:
        check("ru 提示词存在", False, str(e))
        return
    check("ru 写明屈折规则（不许还原成词典形）",
          "Do NOT restore the nominative" in system)
    check("ru 写明 ё/е 照抄规则", "`ё` vs `е`" in system)
    check("ru 写明上游错字照抄规则",
          "upstream transcription errors" in system)
    check("ru 保留 tab 必须照抽的硬要求",
          "Sensitive material is in scope" in system)
    check("ru 保留 other 互斥规则", "`other` is exclusive" in system)


def _nominative(check) -> None:
    """词典形（一格）还原：纯函数 + 内存造的批次，不调 API、不花钱。

    覆盖词典形一格还原的判据表，外加两条最要紧的回归：
      * 锚定闸门没有被放宽（词典形走独立字段，不是塞进 find_verbatim）
      * 西语/法语走同一段代码时 `*_base` 恒空 -> xlsx 逐字节不变
    """
    from .corpus import Sentence
    from .extract import (
        TermRow, check_nominative, foreign_side, head_group, load_morph,
        nom_words, russian_side, validate_batch,
    )
    check("russian_side 是 foreign_side 的别名（兼容不拆）",
          russian_side is foreign_side)

    morph = load_morph()
    has_morph = morph is not None
    check("pymorphy3 状态可报告（装了就启用，没装只降级不报错）", True,
          f"morph={'启用' if has_morph else '未启用'}")

    # ---- 该通过的：9 行对照表（提示词里的示例表就是这一张）
    OK = [
        ("внеблоковый статус", "внеблокового статуса", "属格->主格，形容词一致"),
        ("Украина", "Украины", "属格->主格"),
        ("ядерный удар", "ядерных ударов", "复数->单数（ru 起数也还原）"),
        ("ядерные удары", "ядерных ударов", "复数->复数也合法（习惯复数由提示词判）"),
        ("совместные учения", "совместных учений", "习惯复数保留"),
        ("дикий кабан", "диких кабанов", "复数->单数，形容词一致"),
        ("коленчатый вал", "коленчатых валов", "复数->单数"),
        ("империя лжи", "империю лжи", "从属属格 лжи 必须保留"),
        ("русофобский киевский режим", "русофобского киевского режима", "两个形容词一致"),
        ("военный блок НАТО", "военного блока НАТО", "НАТО 不变格"),
        ("судоподъёмник", "судоподъёмнику", "与格->主格，ё 保留"),
        ("политические заверения", "политические заверения", "本来就是主格，原样"),
        ("война с нацизмом", "войне с нацизмом", "介词短语，нацизмом 保留工具格"),
        ("искусственный интеллект", "искусственного интеллекта", "属格->主格"),
    ]
    for base, anch, why in OK:
        err = check_nominative(base, anch, morph=morph)
        check(f"词典形通过: {base}  ← {anch}（{why}）", err is None, f"报错 {err}")

    # ---- 该失败的
    BAD = [
        ("санкции", "внеблокового статуса", "换了词，词干不符"),
        ("внеблоковый статус Украины", "внеблокового статуса", "词数增加"),
        ("внеблоковый status", "внеблокового статуса", "引入拉丁字母"),
        ("不结盟地位", "внеблокового статуса", "引入汉字（顺手翻译）"),
        ("", "внеблокового статуса", "ru_nom 为空"),
    ]
    for base, anch, why in BAD:
        err = check_nominative(base, anch, morph=morph)
        check(f"词典形判失败: {base!r}（{why}）", err is not None, "居然通过了")

    # ---- 不得把单数改成复数，方向不能搞反
    PLURALIZED = [
        ("ядерные удары", "ядерный удар", "单数原文被改成复数"),
        ("дикие кабаны", "дикий кабан", "同上"),
    ]
    for base, anch, why in PLURALIZED:
        err = check_nominative(base, anch, morph=morph)
        if has_morph:
            check(f"复数化被抓住: {base} ← {anch}（{why}）", err is not None,
                  "居然通过了")
        else:
            check(f"缺库时放过复数化（降级只验关系）: {base}", err is None, f"报错 {err}")
    check("同形歧义不被误判为复数化: экосистема ← экосистемы",
          check_nominative("экосистема", "экосистемы", morph=morph) is None,
          "экосистемы 有主格复数同形，不该判失败")

    # ---- 「压根没还原」必须被抓住 —— 这是形态校验的唯一价值
    NOT_RESTORED = [
        ("Украины", "属格单数，且 Украина 是 Sgtm，不存在主格复数同形"),
        ("искусственного интеллекта", "两个词都没有主格解"),
        ("холодной войны", "войны 有主格复数同形，但 холодной 没有主格解"),
    ]
    for form, why in NOT_RESTORED:
        err = check_nominative(form, form, morph=morph)
        if has_morph:
            check(f"没还原被抓住: {form}（{why}）", err is not None, "居然通过了")
        else:
            check(f"缺库时放过（降级只验关系）: {form}", err is None, f"报错 {err}")

    # ---- 降级路径必须可达且行为明确：强制不用形态学时，「没还原」会被放过
    check("降级路径可达：use_morph=False 时 Украины/Украины 通过",
          check_nominative("Украины", "Украины", use_morph=False) is None)
    check("降级路径仍然挡住换词",
          check_nominative("санкции", "Украины", use_morph=False) is not None)

    # ---- 中心组的边界：从属成分必须被排除在主格要求之外
    if has_morph:
        check("中心组止于第一个名词：империя лжи -> ['империя']",
              head_group(nom_words("империя лжи"), morph) == ["империя"])
        check("中心组含前置修饰语：внеблоковый статус -> 两词",
              head_group(nom_words("внеблоковый статус"), morph)
              == ["внеблоковый", "статус"])

    # ---- 切词：连字符留在词内，边缘标点剥掉
    check("nom_words 连字符不断词",
          nom_words("военно-политические мини-альянсы")
          == ["военно-политические", "мини-альянсы"],
          f"实际 {nom_words('военно-политические мини-альянсы')}")
    check("nom_words 剥边缘标点",
          nom_words("«империю лжи»,") == ["империю", "лжи"],
          f"实际 {nom_words('«империю лжи»,')}")

    # ---- 锚定闸门（与 _pure 里那几条互为镜像，刻意重复）
    check("闸门未放宽：词典形仍然锚不上原句",
          find_verbatim("внеблоковый статус", S_UKR) is None)

    # ---- 方向判定：俄语在哪一侧不固定
    s_ru_src = Sentence(pos=1, out_id=1, src=S_UKR,
                        tgt="我们提出是不改变乌克兰的不结盟地位。")
    s_ru_tgt = Sentence(pos=1, out_id=1, src="生船机才能得以平稳升降",
                        tgt="судоподъёмнику нужно плавно подниматься")
    check("russian_side 认出俄语在 src", russian_side(s_ru_src) == "src")
    check("russian_side 认出俄语在 tgt", russian_side(s_ru_tgt) == "tgt")

    # ---- TermRow 的交付取值：默认回落，填了才用词典形
    r0 = TermRow(sent_id=1, src_text="a", tgt_text="b", term_src="внеблокового статуса",
                 term_tgt="不结盟地位", types="poli")
    check("TermRow 默认 *_base 为空", r0.term_src_base == "" and r0.term_tgt_base == "")
    check("默认回落到原句切片",
          r0.out_src == "внеблокового статуса" and r0.out_tgt == "不结盟地位")
    r0.term_src_base = "внеблоковый статус"
    check("填了 base 就交 base", r0.out_src == "внеблоковый статус")
    check("另一侧不受影响", r0.out_tgt == "不结盟地位")

    # ---- validate_batch：俄语侧被填，中文侧永远留空
    def one(dict_form, sent=s_ru_src, lang="ru", key="dict_form"):
        items = [{"sent_id": 1, "terms": [{
            "term_src": "внеблокового статуса" if sent is s_ru_src else "生船机",
            "term_tgt": "不结盟地位" if sent is s_ru_src else "судоподъёмнику",
            key: dict_form, "types": ["poli"]}]}]
        return validate_batch(items, [sent], lang=lang)

    oc = one("внеблоковый статус")
    check("validate_batch(ru) 收下词典形", oc.nom_ok == 1 and oc.nom_fallback == 0,
          f"ok={oc.nom_ok} fb={oc.nom_fallback} {oc.nom_failures}")
    row = oc.rows[0] if oc.rows else None
    check("俄语侧填 term_src_base", row is not None
          and row.term_src_base == "внеблоковый статус")
    check("锚定值仍是原句切片", row is not None
          and row.term_src == "внеблокового статуса")
    check("中文侧 *_base 恒空（中文没有屈折）", row is not None and row.term_tgt_base == "")

    oc = one("судоподъёмник", sent=s_ru_tgt)
    check("zh-ru 方向填 term_tgt_base",
          oc.nom_ok == 1 and oc.rows[0].term_tgt_base == "судоподъёмник"
          and oc.rows[0].term_src_base == "",
          f"ok={oc.nom_ok} {oc.nom_failures}")

    # ---- 回落：不合格**不丢术语**
    oc = one("санкции")
    check("词典形不合格时回落而不丢术语",
          len(oc.rows) == 1 and oc.nom_fallback == 1 and oc.nom_ok == 0,
          f"rows={len(oc.rows)} fb={oc.nom_fallback}")
    check("回落后交付值 = 原句切片",
          oc.rows and oc.rows[0].out_src == "внеблокового статуса")
    check("回落原因进 nom_failures（要上报告）", len(oc.nom_failures) == 1)
    check("回落也进 failures（触发一次纠正重试）",
          any("dict_form" in x for x in oc.failures), f"实际 {oc.failures}")

    oc = one("")
    check("缺 dict_form 字段也是回落，不丢术语",
          len(oc.rows) == 1 and oc.nom_fallback == 1)

    # ---- 旧键 ru_nom 必须继续认：ru 的提示词与已有的响应缓存都用那个键
    oc = one("внеблоковый статус", key="ru_nom")
    check("兼容旧键 ru_nom（ru 的缓存不作废）",
          oc.nom_ok == 1 and oc.rows[0].term_src_base == "внеблоковый статус",
          f"ok={oc.nom_ok} {oc.nom_failures}")

    # ---- 不收词典形的语种（如日语，尚未接入）必须完全不碰这条支路
    oc = one("внеблоковый статус", lang="ja")
    check("lang 不在 DICT_FORM_LANGS 时忽略该键，*_base 恒空",
          oc.nom_ok == 0 and oc.nom_fallback == 0
          and oc.rows[0].term_src_base == "" and oc.rows[0].term_tgt_base == "",
          f"ok={oc.nom_ok} fb={oc.nom_fallback}")
    check("该语种交付值 = 原句切片",
          oc.rows[0].out_src == "внеблокового статуса")
    check("该语种不会因为这个键触发纠正重试", not oc.failures, f"实际 {oc.failures}")

    # ---- 俄语用的是俄语校验器，不会被西语那套判据串台
    oc = one("внеблоковый статус", lang="ru")
    check("lang=ru 走 check_nominative（而不是 Romance 那套）", oc.nom_ok == 1)


def _prompt_v4(check) -> None:
    """ru：词典形一节要在，且不能把已有的规则改丢。"""
    from .extract import load_prompt

    try:
        system, _, phash = load_prompt("ru")
    except FileNotFoundError as e:
        check("ru 提示词存在", False, str(e))
        return
    check("ru 写明保持单复数",
          "KEEP THE NUMBER THE SENTENCE USED" in " ".join(system.split()))
    check("ru 写明从属名词保留自己的格",
          "A subordinate noun keeps its own case." in system)
    check("ru 写明词数不得增减", "Never change the number of words." in system)
    check("ru 明说主格不等于词典形（复数仍要还原）",
          "plural -> singular" in system)
    # 已有的规则必须还在
    check("ru 保留 tab 的「命名 vs 定性」界线",
          "does the span merely NAME something, or does it PASS JUDGMENT" in system)
    check("ru 保留「不是每个名词都是术语」", "Not every noun is a term" in system)
    check("ru 保留跨度必须完整",
          "The span must be a well-formed phrase" in system)
    check("ru 保留 tab 必须照抽", "Sensitive material is in scope" in system)
    check("ru 保留 other 互斥", "`other` is exclusive" in system)
    # 屈折照抄这条不能删，但必须改成「仅限这两个字段」，否则与 ru_nom 自相矛盾
    check("ru 的屈折照抄限定在两个字段内（不与 ru_nom 打架）",
          "Do NOT restore the nominative or\n  dictionary form in these two fields."
          in system)


def _prompt_v5(check) -> None:
    """ru：键名是 dict_form、数也要还原；已有的规则一条都不能丢。"""
    from .extract import load_prompt

    system, _u, phash = load_prompt("ru")
    check("ru 声明 dict_form 输出键", "dict_form" in system)
    check("ru 不再用旧键名 ru_nom", "ru_nom" not in system)
    check("ru 引 ISO 10241-1 作为依据", "ISO 10241-1" in system)
    check("ru 引 IATE 手册作为依据", "IATE handbook" in system)
    check("ru 要求中心词也还原到单数",
          "Also put the head in the SINGULAR." in system)
    check("ru 明写习惯复数保留",
          "HABITUALLY USED in the plural" in system)
    check("ru 举了习惯复数的实例 совместные учения",
          "`совместные учения`" in system)
    check("ru 举了 права человека", "права человека" in system)
    check("ru 的对照表把 ядерных ударов 改成单数",
          "| `ядерных ударов` | `ядерный удар` |" in system)
    check("ru 收窄了「已是主格就原样」（主格不等于词典形）",
          "A nominative plural is NOT automatically the dictionary form" in system)
    # 这些已有规则必须还在
    check("ru 保留从属名词保留自己的格",
          "A subordinate noun keeps its own case." in system)
    check("ru 保留词数不得增减", "Never change the number of words." in system)
    check("ru 的屈折照抄仍限定在两个字段内",
          "in these two fields." in system)
    # 这些已有规则同样不能丢
    check("ru 保留 tab 的「命名 vs 定性」界线",
          "does the span merely NAME something, or does it PASS JUDGMENT" in system)
    check("ru 保留「不是每个名词都是术语」", "Not every noun is a term" in system)
    check("ru 保留跨度必须完整",
          "The span must be a well-formed phrase" in system)
    check("ru 保留 tab 必须照抽", "Sensitive material is in scope" in system)
    check("ru 保留 other 互斥", "`other` is exclusive" in system)


def run(check) -> None:
    _pure(check)
    # 语料不在就跳过这两块；`_prompt*` / `_nominative` 只读提示词与字面串，照跑。
    if corpus_dir("ru_test").exists():
        _corpus(check)
        _corpus_full(check)
    else:
        print("  [跳过] 俄语语料层断言（_corpus / _corpus_full）—— 未提供语料")
    _prompt(check)
    _nominative(check)
    _prompt_v4(check)
    _prompt_v5(check)


if __name__ == "__main__":
    import sys as _sys
    print("本模块不独立运行 —— 请用 `python -m getterms.tests`。", file=_sys.stderr)
    _sys.exit(2)
