"""法语轮次的断言，单独一个文件。

为什么独立成文件：法语与俄语两轮在不同终端并行推进，改同一个 `tests.py`
会一直撞合并冲突。`tests.py` 只调一次 `tests_fr.run(check, es_files)`。

覆盖 `计划_法语.md` §1 的四类新缺陷 + §7 的验证表，外加「三条新规则在西语上
一次都不触发」的回归断言。
用法：由 `python -m getterms.tests` 自动带上，不单独跑。
"""
from __future__ import annotations

import json

from .config import corpus_dir
from .corpus import (
    dedup_by_md5, load_corpus, looks_like_srt_ts, out_name_for, parse_file,
    repair_json, replace_out_name, zip_overlaps,
)


def _pure(check) -> None:
    """纯函数层：不依赖语料，随时可跑。"""
    # ---- JSON 语法定点修复（法语 6 个文件；见 §1.2）
    d, fx = repair_json('{"a": 1, "sentences": [{"sent_id": 1}]}')
    check("repair_json 对正常 JSON 零改动", d is not None and fx == [], f"fixes={fx}")

    nl = chr(10)
    miss = ("{" + nl + '  "pair": "zh-fr",' + nl + '  "session_no": "0035"' + nl
            + '  "seg_no": "seg001",' + nl + '  "sentences": []' + nl + "}")
    d, fx = repair_json(miss)
    check("repair_json 补回缺失的逗号（上游真实 bug 的最小复现）",
          d is not None and d.get("seg_no") == "seg001", f"got={d} fixes={fx}")
    check("repair_json 记录了修复动作", len(fx) == 1 and "逗号" in fx[0], f"fixes={fx}")

    d, fx = repair_json('{"a": [1, 2,]}')
    check("repair_json 删掉多余的尾逗号", d == {"a": [1, 2]}, f"got={d} fixes={fx}")

    d, fx = repair_json('{"a": 1 "b": 2 "c": 3}')
    check("repair_json 能连修多处缺逗号", d == {"a": 1, "b": 2, "c": 3},
          f"got={d} fixes={fx}")
    # 缺的是冒号不是逗号 —— 不在我们认的两种错里，必须老实放弃，不能乱改
    d, fx = repair_json('{"a": 1, "b" 2}')
    check("repair_json 不去猜「缺冒号」这种错，老实放弃并说明原因",
          d is None and any("无法自动修复" in x for x in fx), f"got={d} fixes={fx}")

    d, fx = repair_json("这根本不是 JSON")
    check("repair_json 修不了就返回 None 并记账", d is None and bool(fx), f"fixes={fx}")

    # ---- 文件名拼写归一化（与 HTML 的第 3 处有意差异；见 §1.4）
    check("replace_out_name 本身仍与 HTML 逐字一致（归一化不许渗进去）",
          replace_out_name("x_algin.qc.json") == "x_algin.qc_term.xlsx")
    for bad, want in (("a_algin.qc.json", "a_term.xlsx"),
                      ("a_aglin.qc.json", "a_term.xlsx"),
                      ("a_align.qc..json", "a_term.xlsx"),
                      ("a_align.qc.json", "a_term.xlsx")):
        got, _ = out_name_for(bad)
        check(f"out_name_for 纠正拼写 {bad}", got == want, f"实际 {got}")
    got, note = out_name_for("a_align.json")
    check("`_align.json`（缺 .qc 但 align 拼对了）不动，照 HTML 规则",
          got == "a_align_term.xlsx" and note is None, f"实际 {got} / {note}")
    _, note = out_name_for("a_algin.qc.json")
    check("纠正拼写会留下说明（要进给王敬的映射表）",
          note is not None and "拼写已纠正" in note)

    # ---- SRT 时间轴识别（字段错位的触发条件；见 §1.3）
    check("识别逗号毫秒的时间轴", looks_like_srt_ts("00:00:00,100 --> 00:00:08,100"))
    check("识别点号毫秒的时间轴", looks_like_srt_ts("00:00:00.220 --> 00:00:04.272"))
    check("正常中文不误判", not looks_like_srt_ts("就在本周，我们踏上了新征程。"))
    check("含时刻的普通句子不误判", not looks_like_srt_ts("会议在 10:30 开始。"))
    check("单个时间戳（没有箭头）不误判", not looks_like_srt_ts("00:00:14,480"))

    # ---- 字段错位还原：两种形态
    def parsed(obj, name="a_align.qc.json"):
        return parse_file(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          name, "z.zip")

    cf = parsed({"sentences": [{
        "sent_id": 1,
        "src": {"start_ms": "1", "end_ms": "", "text": "00:00:00,100 --> 00:00:08,100"},
        "tgt": {"start_ms": "我们踏上了新征程。", "end_ms": "", "text": "Nous avons commencé."},
        "notes": ""}]})
    s = cf.sentences[0]
    check("形态 1 还原：中文进 src 槽、法文进 tgt 槽",
          s.src == "我们踏上了新征程。" and s.tgt == "Nous avons commencé.",
          f"src={s.src!r} tgt={s.tgt!r}")
    check("形态 1 计入 shifted_recovered", cf.shifted_recovered == 1)
    check("形态 1 还原后该句可用（不还原就会被静默过滤）", s.usable)
    check("形态 1 记了「建议复查」", any("建议复查" in n for n in cf.notes), f"{cf.notes}")

    cf = parsed({"sentences": [{
        "sent_id": 1,
        "src": {"start_ms": "1", "end_ms": "", "text": "00:00:01,000 --> 00:00:05,266"},
        "tgt": {"start_ms": "中国每年发生海难事件。", "end_ms": "",
                "text": "00:00:00,000 --> 00:00:05,270"},
        "notes": "Des accidents en mer se produisent."}]})
    s = cf.sentences[0]
    check("形态 2 还原：法文从 notes 取回",
          s.src == "中国每年发生海难事件。" and s.tgt == "Des accidents en mer se produisent.",
          f"src={s.src!r} tgt={s.tgt!r}")
    check("形态 2 计入 shifted_recovered", cf.shifted_recovered == 1)

    cf = parsed({"sentences": [{
        "sent_id": 1,
        "src": {"start_ms": "1", "text": "00:00:01,000 --> 00:00:05,266"},
        "tgt": {"start_ms": "", "text": "00:00:00,000 --> 00:00:05,270"},
        "notes": ""}]})
    check("判不出来就不硬猜，不计入已还原", cf.shifted_recovered == 0)
    check("判不出来的句子记了账", any("无法还原" in n for n in cf.notes), f"{cf.notes}")
    check("判不出来的句子不送模型", not cf.usable_sentences)

    # 正常文件绝不能被这条规则碰到
    cf = parsed({"sentences": [{
        "sent_id": 1,
        "src": {"start_ms": "00:00:14,480", "end_ms": "00:00:19,280", "text": "现在有了新帮手。"},
        "tgt": {"start_ms": "00:00:00,000", "end_ms": "00:00:06,980", "text": "Un nouveau soutien."},
        "notes": ""}]})
    check("结构正常的文件不触发还原", cf.shifted_recovered == 0
          and cf.sentences[0].src == "现在有了新帮手。")


def _corpus(check, es_files) -> None:
    """真实语料层。法语目录不在就只报一条，不让整轮断言挂掉。"""
    fr_dir = corpus_dir("fr")
    if not fr_dir.exists():
        check("法语语料目录存在", False, f"{fr_dir} 不存在，跳过法语语料断言")
        return

    files, skipped = load_corpus(fr_dir)
    by = {f.basename: f for f in files}
    check("法语去重后 555 个文件", len(files) == 555, f"实际 {len(files)}")

    # ---- §1.1 跨 zip 重名
    ov = zip_overlaps(fr_dir)
    check("法语只有 2 对 zip 有交集", len(ov) == 2, f"实际 {len(ov)}")
    check("两对都是严格包含关系（所以「取超集」这条规则站得住）",
          all("⊂" in r for _, _, _, r in ov), f"实际 {[r for *_, r in ov]}")
    check("重名已去重并写进 skipped",
          any("跨 zip 重名 57" in s for s in skipped), f"{skipped}")

    f2 = by.get("zh-fr_tour_serv_0006_seg002_align.qc.json")
    check("冲突文件 seg002 取到校对版（以船为家，不是错字「以船为驾」）",
          f2 is not None
          and any("以船为家" in s.src for s in f2.sentences)
          and not any("以船为驾" in s.src for s in f2.sentences))
    f3 = by.get("zh-fr_tour_serv_0006_seg003_align.qc.json")
    check("冲突文件 seg003 取到 159 句那版（不是 166 句）",
          f3 is not None and len(f3.sentences) == 159,
          f"实际 {len(f3.sentences) if f3 else None}")
    check("冲突文件 seg003 取到校对版（贬谪…流放，不是「贬折…留放」）",
          f3 is not None and any("贬谪" in s.src and "流放" in s.src for s in f3.sentences))

    # ---- §1.2 JSON 语法修复
    rep = [f for f in files if any("定点修复" in n for n in f.notes)]
    check("法语 6 个文件的 JSON 语法被修好", len(rep) == 6, f"实际 {len(rep)}")
    check("修好的都是 conf_poli", all("conf_poli" in f.basename for f in rep),
          f"{[f.basename for f in rep]}")
    f11 = by.get("zh-fr_conf_poli_0011_seg002_align.qc.json")
    check("HTML 会整文件丢掉的那个，我们读出 123 句",
          f11 is not None and len(f11.sentences) == 123,
          f"实际 {len(f11.sentences) if f11 else None}")
    check("那个文件修了两处（补逗号 + 删尾逗号）",
          f11 is not None and any("删一个多余的逗号" in n for n in f11.notes),
          f"{f11.notes if f11 else None}")
    check("没有文件是靠正则兜底读出来的（都被正经修好了）",
          not any("退回正则" in n for f in files for n in f.notes))
    check("法语 555 个文件全是 standard schema（修好 JSON 后没有异种）",
          all(f.schema == "standard" for f in files),
          f"异种 {[(f.basename, f.schema) for f in files if f.schema != 'standard'][:5]}")

    # ---- §1.3 字段错位还原
    sh = [f for f in files if f.shifted_recovered]
    check("法语 11 个文件有字段错位", len(sh) == 11, f"实际 {len(sh)}")
    check("共还原 1528 句", sum(f.shifted_recovered for f in sh) == 1528,
          f"实际 {sum(f.shifted_recovered for f in sh)}")
    check("还原后方向全部正确（zh-first）",
          all(f.content_direction() == "zh-first" for f in sh),
          f"{[(f.basename, f.content_direction()) for f in sh if f.content_direction() != 'zh-first']}")
    fe = by.get("zh-fr_conf_econ_0010_seg003_align.qc.json")
    check("形态 1 真实文件：第 1 句 src 中文 / tgt 法语",
          fe is not None and fe.sentences[0].src.startswith("就在本周")
          and fe.sentences[0].tgt.startswith("Cette semaine"))
    ft = by.get("zh-fr_conf_tech_0009_seg004_align.qc.json")
    check("形态 2 真实文件：法文确实从 notes 取回",
          ft is not None and "accidents en mer" in ft.sentences[0].tgt)
    check("错位文件还原后全部可用（1528 句一句都没丢）",
          all(len(f.usable_sentences) == len(f.sentences) for f in sh),
          f"{[(f.basename, len(f.usable_sentences), len(f.sentences)) for f in sh if len(f.usable_sentences) != len(f.sentences)]}")

    # ---- §1.4 文件名
    ren = [f for f in files if any("拼写已纠正" in n for n in f.notes)]
    check("法语 7 个文件名拼写被纠正", len(ren) == 7, f"实际 {len(ren)}")
    check("纠正后输出名干净（无 algin/aglin/.qc_）",
          all(f.out_name.endswith("_term.xlsx") and "algin" not in f.out_name
              and "aglin" not in f.out_name and ".qc_" not in f.out_name for f in ren),
          f"{[f.out_name for f in ren]}")
    plain = [f for f in files if f.basename.endswith("_align.json")]
    check("23 个 `_align.json` 保持不动", len(plain) == 23, f"实际 {len(plain)}")
    check("`_align.json` 的输出名照 HTML 规则带 _align",
          all(f.out_name.endswith("_align_term.xlsx") for f in plain))
    check("法语输出名零碰撞",
          len({f.out_name for f in files}) == len(files),
          f"唯一 {len({f.out_name for f in files})} / 文件 {len(files)}")
    bad_char = [f.out_name for f in files
                if any(c in f.out_name for c in '<>:"/|?*')]
    check("法语输出名无 Windows 非法字符", not bad_char, f"{bad_char[:3]}")

    # ---- 规模与分层
    tot = sum(len(f.sentences) for f in files)
    us = sum(len(f.usable_sentences) for f in files)
    check("法语总句对 65162", tot == 65162, f"实际 {tot}")
    check("法语可用句对 65097", us == 65097, f"实际 {us}")
    check("法语没有 md5 完全相同的组", len(dedup_by_md5(files)) == len(files),
          f"唯一内容 {len(dedup_by_md5(files))} / 文件 {len(files)}")
    check("法语 sent_id 全部干净，无需重编",
          not any("重编" in n for f in files for n in f.notes))
    check("法语无 0 句文件", all(f.sentences for f in files),
          f"{[f.basename for f in files if not f.sentences]}")

    lay: dict[str, int] = {}
    for f in files:
        lay[f.layer()] = lay.get(f.layer(), 0) + len(f.usable_sentences)
    want = {"conf/poli": 13940, "tour/scen": 13798, "tour/attr": 8689,
            "tour/serv": 7916, "tour/muse": 7800, "conf/econ": 6673,
            "conf/tech": 6281}
    check("法语 7 个层（比西语多 tour/scen）", len(lay) == 7, f"实际 {sorted(lay)}")
    for k, v in want.items():
        check(f"法语层 {k} 可用句 {v}", lay.get(k) == v, f"实际 {lay.get(k)}")

    # ---- §1.6 绝不按 session 去重
    a = by.get("fr-zh_conf_poli_0001_seg001_align.qc.json")
    b = by.get("zh-fr_conf_poli_0001_seg001_align.qc.json")
    check("同 session 号的两个方向都在（没按 session 误去重）",
          a is not None and b is not None)
    if a and b:
        ta = {s.src for s in a.usable_sentences} | {s.tgt for s in a.usable_sentences}
        tb = {s.src for s in b.usable_sentences} | {s.tgt for s in b.usable_sentences}
        check("这两个文件文本零重合（确实是不同讲话，不是修订版）",
              not (ta & tb), f"重合 {len(ta & tb)} 句")

    # ---- 西语零回归：三条新规则在西语上一次都不该触发
    check("西语没有文件触发 JSON 定点修复",
          not any("定点修复" in n for f in es_files for n in f.notes))
    check("西语没有文件触发字段错位还原",
          not any(f.shifted_recovered for f in es_files))
    check("西语没有文件触发文件名纠正",
          not any("拼写已纠正" in n for f in es_files for n in f.notes))
    check("西语 zip 之间零重名", not zip_overlaps(corpus_dir("es")))


def run(check, es_files) -> None:
    _pure(check)
    # ⚠ **公开副本专有**：语料不在就**可见地跳过**语料层，不当成失败。
    #   （内部仓库里有语料，照跑。这里不是一个「跳过记成通过」——
    #   它当场打印，读输出的人分得出测过和没测。）
    if corpus_dir("fr").exists():
        _corpus(check, es_files)
    else:
        print("  [跳过] 法语语料层断言 —— 公开副本不含语料（内容涉第三方版权）")


if __name__ == "__main__":
    # ⚠ 同 tests_ru：本模块只定义 `run(check, es_files)`，由 `tests.py` 调用。
    #   以前 `python -m getterms.tests_fr` 会**静默退出 0**，看起来像通过。
    import sys as _sys
    print("本模块不独立运行 —— 请用 `python -m getterms.tests`（它会调 tests_fr.run）。",
          file=_sys.stderr)
    _sys.exit(2)
