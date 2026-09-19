"""生成给语言老师看的一页纸小样（`--lang es|fr|ru`）。

目的：她 3~5 分钟看完就能拍板。所以：
  * 只挑十几句，不给她 900 行的分歧表；
  * 每句显示完整原文（中文一行、外语一行），紧跟着这句抽出的术语；
  * **留一列让她直接打判断**（对 / 删 / 改类型），她标完回传就是我们的 gold set 第一版；
  * 必须包含时政敏感句 —— 那是这个项目最需要她确认的地方；
  * 中文/外语哪边是 src 由 CJK 占比判定后**显式标注**，不让她猜。

数据来源：bake-off 里 gemini-3.7-flash @ high 的真实结果（用户已定此配置），
不重新调用 API。

用法：python -m getterms.small_sample --lang fr --source terms_xxx.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import BAKEOFF_DIR, FINAL_DUMPS, OUT_DIR
from .corpus import cjk_ratio
from .extract import DICT_FORM_LANGS

# ⚠ 这里曾经写死成 `terms_gemini-3_7-flash__high__b10.json` —— 一个**无版本后缀的
#   v0 基线**。而三语的层名相同，于是不带 `--source` 跑 `--lang ru` 会静默产出
#   「内容是西语 v0、文件名叫小样_俄语」的表，一句报错都没有。
#   现在默认读 `config.FINAL_DUMPS[lang]`（与 selfcheck / span_defects / probe 同源），
#   并在 main() 里核对 dump 的语种身份。
#   同一个坑此前已在 span_defects 的默认目标、FINAL_DUMPS 落后一代、
#   PER_LAYER_BY_LANG["ru"] 的旧 4 层名单上各犯过一次 —— 这是第四次。
SOURCE = ""          # main() 会按 --lang 填成 FINAL_DUMPS[lang]

# 每层挑几句；conf/poli 多给，因为敏感词判定最需要老师确认。
# 法语多一层 tour/scen（口语旅拍，占语料 21%），给 3 句 —— 那一层最容易过抽，
# 需要老师确认「口头语到底要不要抽」。
PER_LAYER_BY_LANG = {
    "es": {"conf/poli": 4, "tour/muse": 3, "conf/econ": 2,
           "conf/tech": 2, "tour/attr": 2, "tour/serv": 2},
    "fr": {"conf/poli": 4, "tour/scen": 3, "tour/muse": 2, "conf/econ": 2,
           "conf/tech": 2, "tour/attr": 1, "tour/serv": 1},
    # ⚠ 必须是**全量语料的 6 层**。这里曾经只有 4 层（旧的 10 文件测试语料），
    #   于是 tour/attr + tour/muse（占俄语术语 34%、音译专名与词典形回落最集中）
    #   整两层没进小样 —— 最该请老师看的部分反而没送审。
    "ru": {"conf/poli": 4, "tour/muse": 3, "conf/econ": 2,
           "conf/tech": 2, "tour/attr": 2, "tour/serv": 2},
}

# 一个文件最多贡献几句。默认 1 —— 一个文件 = 一段录音 = 一个讲者一个话题，
# 取多了小样就变成「看一个人怎么说话」。
# 俄语例外给 2：bake-off 的分层样本每层只抽了 **2 个文件**，卡 1 句/文件的话
# 6 层加起来最多 12 句，凑不满 15 句的规模。（原注释写的「测试语料只有 10 个
# 文件」已经过期 —— 俄语语料 2026-09-18 换成全量 533 文件，限制来自样本不是语料。）
MAX_PER_FILE_BY_LANG = {"es": 1, "fr": 1, "ru": 2}

# 必须出现在小样里的条目，按术语字符串锁定。用途：把**真正需要老师拍板的争议项**
# 钉进样本，不能靠随机取样碰运气。找不到的 needle 静默跳过（会在末尾汇报）。
MUST_INCLUDE_BY_LANG = {
    "es": [
        # 本轮要西语老师确认的三类，一个都不能靠随机取样碰运气：
        # ① 该还原成单数的（她自己举的例子就是 `suizos`）
        "suizos", "impuestos", "instituciones sanitarias",
        # ② 该**保留**复数的：习惯复数与专名。她担心的「还原会失去术语含义」就是这类
        "derechos humanos", "Estados Unidos",
        # ②b 本轮**新修好**的：上一版被压成单数，这一版保留复数。出处见 plural_lexicon
        "palillos", "masas populares",
        # ③ 动词术语：她问「是不是指令规避了动词」，把实测存在的那几条摆给她看
        "profundizar la reforma",
    ],
    "fr": [
        # ⚠ 这里原有 `forces terroristes`、`dettes en souffrance`、`droits de l'homme`
        #   三条 —— 它们在 fr_v7 这份 dump 里**连句子都不存在**（出自更早一版的样本），
        #   等于空操作，而说明文字还拿它们当「本轮变化」的例子，老师会找不到证据。
        "puissances étrangères", "taux d'intérêt hypothécaires",
        # 老师 2026-09-18 补的规则②③两个方向都要在表里能看到，否则她没法复核：
        #   ③ 表构成 -> 保留复数
        "revenus des ménages",
        #   ② 表类别 -> 变单数（她**纠正**我们的那一句，必须能看见）
        "aliments à base de farines",
        # ⚠ 她纠正的那个词就是 `entreprise`（「从属的 entreprise 需要相应变成单数，
        #   去掉 s，因为是类别定语」）。上一份小样 48 条里 **`entreprise` 一次都没出现**
        #   —— 她要复核的正是这一条，表里却看不到，只能凭信任点头。必须进表。
        "nouvelles formes d'entreprise",
        # ⚠ 这里一度加过 `recettes fiscales`（注解里的第三条引证），但它和问题一点名的
        #   `réseaux hydrographiques` 同在 tour/attr（配额 1），把后者挤出了表 ——
        #   等于为了注解里的第三条，弄丢了问题里的例子。取舍：**问题里点名的优先**。
        "Nations Unies",
        # 本轮**新修好**的：上一版被压成单数，这一版保留复数
        "objectifs de développement durable", "nouilles instantanées",
    ],
    "ru": [
        # 口径一（老师 2026-09-17 已确认通过）：固化机构名/时期名不标 tab，
        # 建立在它之上的定性才标。留在样本里是为了让她看到口径已落地。
        "НАТО", "холодной войны", "Монро",
        # 口径二（她已确认「还原到一格」对）+ 本轮的增量（数也还原到单数）
        "Украины",
        # 本轮**数被还原**的实例：让老师在表里能直接看到「数也还原了」这句说明的证据。
        # ⚠ 这里原有 `империю лжи`、`диких кабанов` 两条 —— 它们出自旧的 ru_test
        #   测试语料，全量 dump 里 0 命中（连句子都不在样本里），等于空操作。
        "королевские семьи", "визитные карточки",
        # 本轮**新修好**的：上一版被压成单数，这一版保留复数（两条都有法律/官方出处）
        "национальны", "родител",
        # ⚠ 这里原有一条 `生船机`（上游 ASR 错字，故意摆出来让老师自己发现）。
        # 俄语语料 2026-09-18 换成全量之后，那句不在 bake-off 分层样本里了
        # （全量 dump 0 命中）→ 删掉。不去别处找个错字硬凑，也不靠推理补位；
        # 真要让老师看 ASR 噪声，应该由 audit 的乱敲清单单独出样本。
    ],
}
MUST_INCLUDE: list[str] = []          # main() 会按 --lang 覆盖
MISSING_NEEDLES: list[str] = []       # pick() 回填：MUST_INCLUDE 里 0 命中的

# 小样开头要老师回答的问题。西/法沿用「只问一个问题」；
# 俄语这一轮问的是**词典形还原**：她 2026-09-17 已经定了「术语库要收原型」，
# 现在要她并排确认还原口径本身对不对。
# ⚠ 上一版这里写的是「我们目前收句中形式…因为那是原句里真实存在的字符串」，
#   那个口径已被她推翻，别照抄回来。
QUESTIONS_BY_LANG = {
    "es": [
        "**问题一：这样区分「该还原」与「不该还原」，对不对？**"
        "您提的问题我们按国际术语规范落地了（ISO 10241-1 §6.2.2：名词条目用单数；"
        "IATE 手册：单数，**除非该术语习惯上以复数使用**）。"
        "所以 `colonos suizos`→`colono suizo`、`impuestos`→`impuesto` 这类还原成单数；"
        "而 `derechos humanos`、`Estados Unidos`、`fuerzas armadas` 这类"
        "**保留复数**，因为复数本身就是词条形式。右边多出的「句中形式」一列是它在"
        "原句里的样子，请并排看一眼这条界线划得对不对。\n\n"
        "> 判据是「**该术语习惯上是否以复数使用**」，**不是**「单数形式存不存在」——"
        "后者太严，会把 `palillos`（筷子，DLE 该义项只立在复数）、"
        "`elecciones`（选举，DLE 标 f. pl.；单数 `elección` 是「选择」）、"
        "`masas populares`（DLE 标 U. m. en pl.；单数 `masa` 首义是物理「质量」）"
        "全压坏。本轮已按 DLE 与 IATE 的标注逐词核过。",
        "**问题二：名词的性我们一律不动，对吗？**"
        "`instituciones sanitarias` 还原成 `institución sanitaria`（形容词跟着中心名词，仍是阴性），"
        "不写成 `institución sanitario`。依据是性属于名词的固有属性"
        "（ISO 10241-1 是**标注**性，不是把性规范掉）—— `la política` 政策 与 "
        "`el político` 政客 本来就是两个词。",
        "**问题三（答复您问的动词）：指令没有规避动词。**"
        "657 条实测结果里动词短语只有 4 条：`深化改革 profundizar la reforma`、"
        "`赢得主动 ganar la iniciativa`、`发电 generar electricidad`、"
        "`防洪 controlar inundaciones`（占 0.6%）。少是因为示例与术语库惯例都是名词性的。"
        "我们**不打算**在指令里主动鼓励抽动词，怕把「推进」「加强」这类普通动词也请进来。"
        "请您看这几条，判断这一类要不要收。",
    ],
    "fr": [
        "**问题一（您上次已首肯，请复核落地效果）：术语列收「词典形」。**"
        "这一版按国际术语规范"
        "（ISO 10241-1 §6.2.2 + IATE 手册）把术语列改收**词典形** —— "
        "`taux d'intérêt hypothécaires`→`taux d'intérêt hypothécaire`、"
        "`réseaux hydrographiques`→`réseau hydrographique`。"
        "右边多出的「句中形式」一列是它在原句里的样子。"
        "类型标注、抽取口径、中文侧**全都没动**。",
        # ⚠ 原名点的 `droits de l'homme`、`arts et métiers traditionnels` 在 fr_v7
        #   这份 dump 里 0 命中（连句子都没有）→ 老师没法在表里核对。换成表里有的。
        "**问题二（您上次已同意，请复核实例）：习惯复数与专名保留原样。**"
        "`Nations Unies`、`Organisation des Nations Unies`、"
        "`objectifs de développement durable` 我们不压成单数，"
        "因为复数本身就是词条形式。\n\n"
        "> 本轮修好了一类：判据原先写成「单数形式不存在才保留复数」，太严。"
        "现改回 IATE 手册的原话「**习惯上以复数使用**」。于是 "
        "`objectifs de développement durable`（联合国官方名）、"
        "`nouilles instantanées`（法兰西学院词典标 le plus souvent au pluriel，"
        "单数 `une nouille` 另有「笨蛋」义）这些不再被压成单数。",
        # ⚠ 这里原写「`de` 后的补语也不动，对吗？」—— 那是老师上一轮**刚纠正过**的
        #   错话（她说类别定语要变单数）。提示词早按她的口径改了，说明文字没跟上，
        #   而且只举了「保留复数」一个方向 —— 她要复核的方向在文案和表里都看不到。
        "**问题三（您上次**纠正**过我们的说法，已按您的口径落地，请复核）："
        "性不动；`de` 后的补语按「表类别 / 表构成」分两种处理。** "
        # ⚠ 上面 `。**` 后面那个空格不能省：紧跟 `**表类别**` 会连成 `。****表类别**`，
        #   markdown 把四个星号解析成一团，小样里就显示成 `****表类别**`。
        "**表类别**（后置名词是类别、材料或用途）**变单数**："
        "`aliments à base de farines` 还原成 `aliment à base de farine`"
        "（`farine` 是材料，不是若干个实体）；"
        "**表构成**（补语是组成那个集合的实体）**保留复数**："
        "`revenus des ménages` 还原成 `revenu des ménages`。"
        "性属于名词的固有属性，一律不动：`puissances étrangères` 还原成 "
        "`puissance étrangère`，不写成 `puissance étranger`。",
    ],
    "ru": [
        "**这一版的变化（请复核，不是重新问）：数也还原到单数了。**"
        "您上次确认了「还原到一格」对、并说「单复数不还原也可以」。"
        "后来西语老师提出同一个问题，我们查了国际术语规范："
        "ISO 10241-1 §6.2.2 要求名词条目用**单数**，IATE 手册同样是单数、"
        "**除非该术语习惯上以复数使用**。为了三个语种口径一致，我们把「数」也还原了："
        "`королевские семьи`→`королевская семья`、`визитные карточки`→`визитная карточка`。",
        "**习惯复数仍然保留（本轮把判据改对了）。**"
        "上一版的判据写成「**只以复数存在**才保留复数」，太严 —— "
        "`учение`、`интерес`、`осадок` 的单数在语法上都合法存在，于是"
        "「联合演习」「国家利益」这类被压成了单数。现改回 IATE 手册的原话："
        "「单数，**除非该术语习惯上以复数使用**」。据此保留复数的例如："
        "`национальные интересы`（总统令第 683/400 号把它列为定义术语，官方文本无单数用例）、"
        "`приемные родители`（家庭法典第 153 条条标题原文即复数）、"
        "`переговоры`（无单数形）、`атмосферные осадки`（乌沙科夫词典标 только мн.）。"
        "专名与缩写（`НАТО`）同样不动。",
        "**格与从属成分的处理没变，您上次已确认。**"
        "`постулата об односторонних действиях`→`постулат об односторонних действиях`，"
        "中心词还原到主格，从属的 `об односторонних действиях` 原样不动。"
        "若您认为「数」这一步不该做、维持上次的口径，说一声我们改回去（改一行配置）。",
    ],
}
QUESTIONS: list[str] = []             # main() 会按 --lang 覆盖

# 这一份是「请她拍板」还是「请她复核」。见 tests 里钉住法语为 verify 的断言。
#   decide —— 有她没表过态的新决策（西语：她提的单复数问题 + 我们按 ISO/IATE 的落地；
#             俄语：「数也还原到单数」是我们主动做的增量，她原话是「不还原也可以」）
#   verify —— 只有已定/已纠正口径的落地效果要她看（法语：她已逐条答过「同意/对的」）
# ⚠ 老师答过的题不要再问一遍，那只是占她的时间。
QUESTIONS_MODE_BY_LANG = {"es": "decide", "fr": "verify", "ru": "decide"}
QUESTIONS_MODE = "decide"             # main() 会按 --lang 覆盖

# 要不要多显示「句中形式」一列。收词典形的语种都要，让老师并排核对还原。
# xlsx 与 md 共用同一个开关，避免两处各判一次、判歪了只有一半表格变。
SHOW_SPAN = True
MAX_PER_FILE = 1                          # main() 会按 --lang 覆盖
PER_LAYER = PER_LAYER_BY_LANG["es"]      # main() 会按 --lang 覆盖

# 语种显示名与全量文件数，只用于给老师看的文案
LANG_NAME = {"es": "西语", "fr": "法语", "ru": "俄语"}
LANG_FILES = {"es": 532, "fr": 555, "ru": 533}
# ⚠ 俄语 2026-09-18 换成全量语料（533 文件 / 57,469 可用句）。原先这里写 10，
#   是旧测试语料的数字；它只在「没有提问文案」的分支里印给老师，等于一颗地雷。
LANG = "es"          # main() 会按 --lang 覆盖
MODEL_LABEL = "gemini-3.7-flash（高推理档）"


def _label_from_source(src: str) -> str:
    """从 terms_<模型>__<档位>__b<批>.json 反推给老师看的模型说明。"""
    stem = src.removeprefix("terms_").removesuffix(".json")
    parts = stem.split("__")
    tier = {"low": "低", "medium": "中", "high": "高", "max": "最高", "xhigh": "超高"}
    if len(parts) >= 2:
        return f"{parts[0].replace('_', '.')}（{tier.get(parts[1], parts[1])}推理档）"
    return stem
# 至少要有这么多句含 tab —— 时政敏感是本项目最大风险，必须让老师看到
MIN_TAB_SENTENCES = 4
# 长度上限放到 260 字：同传句子本来就长，且对齐后两侧长度常不对称
# （实测有 src 78 字 / tgt 354 字的）。**先前设 120 把几乎所有敏感句都挡掉了。**
LEN_MIN, LEN_MAX = 12, 260

# 显式指定西文字体。**不指定的话西里尔字母会落到宋体，被渲染成全角**
# （实测：`Продвижение` 显示成 `П р о д в и ж е н и е`，老师根本没法读）。
# 中文会由 Excel 自动回退到本地中文字体，不受影响。
BODY_FONT = "Calibri"
HDR = Font(bold=True, size=11, name=BODY_FONT)
BODY = Font(size=11, name=BODY_FONT)
FILL_HDR = PatternFill("solid", fgColor="DDEBF7")
FILL_SENT = PatternFill("solid", fgColor="FFF9E6")
FILL_ASK = PatternFill("solid", fgColor="E8F5E9")
# 还原过的行（词典形 ≠ 句中形式）用浅橙标出来 —— 老师这次要核的就是这些条目，
# 全表几十条里只有十几条动过，不标出来等于让她逐行比对两列字符串。
FILL_DIFF = PatternFill("solid", fgColor="FFE0B2")
DIFF_FONT = Font(size=11, bold=True, name=BODY_FONT)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def load_sentences() -> dict:
    p = BAKEOFF_DIR / SOURCE
    if not p.exists():
        raise SystemExit(f"找不到 {p}，先跑 bakeoff --step1")
    rows = json.loads(p.read_text(encoding="utf-8"))
    by_sent: dict[tuple, list] = collections.defaultdict(list)
    for r in rows:
        by_sent[(r["layer"], r["file"], str(r["sent_id"]))].append(r)
    return by_sent


def pick(by_sent: dict) -> list[tuple]:
    """确定性挑句：每层给配额，含 tab 的优先，并保证全局至少 MIN_TAB_SENTENCES 句带 tab。

    ⚠ 教训：先前把长度上限设成 120 字，结果几乎把所有含 tab 的句子都挡掉了
    （时政发言长、且对齐后两侧长度不对称）—— 等于把最该给老师看的那一类筛没了。
    """
    def usable(v) -> bool:
        s = v[0]
        return (2 <= len(v) <= 8
                and LEN_MIN <= len(s["src"]) <= LEN_MAX
                and LEN_MIN <= len(s["tgt"]) <= LEN_MAX)

    def has_tab(v) -> bool:
        return any("tab" in r["types"] for r in v)

    chosen: list[tuple] = []
    used_files: collections.Counter = collections.Counter()

    # 先锁定必须出现的争议条目，再用剩余配额按层补齐。
    # 不这么做就只能靠随机取样碰运气碰到 НАТО / 生船机 这些要老师拍板的条目。
    forced_layers: collections.Counter = collections.Counter()
    missing_needles: list[str] = []           # MUST_INCLUDE 里在本 dump 中 0 命中的

    def hay(r) -> str:
        """needle 要在**交付值和原句切片两套值**里找。

        ⚠ 俄语交付词典形之后，`term_src` 里已经是 `Украина` 了，
        只匹配交付值的话 needle `Украины` 会静默失配、样本里就少了那一条。
        """
        return "".join(str(r.get(k, "")) for k in
                       ("term_src", "term_tgt", "term_src_span", "term_tgt_span"))

    for needle in MUST_INCLUDE:
        cands = [(k, v) for k, v in by_sent.items()
                 if any(needle in hay(r) for r in v)
                 and (k, v) not in chosen]
        cands = [c for c in cands if c[0] not in {x[0] for x in chosen}]
        if not cands:
            # ⚠ 绝不静默跳过。MUST_INCLUDE 的全部作用就是保证老师一定看到这几条，
            #   0 命中意味着「我以为送审了、其实没送」。换语料/换 dump 之后最容易发生。
            missing_needles.append(needle)
            continue
        # 优先取术语多、且该文件还没被用满的
        cands.sort(key=lambda kv: (used_files[kv[0][1]], -len(kv[1]), kv[0][1]))
        k, v = cands[0]
        chosen.append((k, v))
        used_files[k[1]] += 1
        forced_layers[k[0]] += 1

    for layer, quota in PER_LAYER.items():
        quota -= forced_layers.get(layer, 0)      # 锁定项已占掉的名额
        if quota <= 0:
            continue
        already = {x[0] for x in chosen}
        pool = [(k, v) for k, v in by_sent.items()
                if k[0] == layer and usable(v) and k not in already]
        # 含 tab 的排前面；其次术语多的；再按文件名与句号稳定排序
        pool.sort(key=lambda kv: (
            0 if has_tab(kv[1]) else 1,
            -len(kv[1]), kv[0][1], int(kv[0][2]) if kv[0][2].isdigit() else 0))
        taken = 0
        for k, v in pool:
            if taken >= quota:
                break
            if used_files[k[1]] >= MAX_PER_FILE:   # 限制单文件贡献，保证话题多样
                continue
            used_files[k[1]] += 1
            chosen.append((k, v))
            taken += 1

    # 全局兜底：若含 tab 的句子不够，从剩余的 tab 句里补，替换掉不含 tab 的最后几句
    n_tab = sum(1 for _, v in chosen if has_tab(v))
    if n_tab < MIN_TAB_SENTENCES:
        extra = [(k, v) for k, v in by_sent.items()
                 if has_tab(v) and usable(v) and used_files[k[1]] < MAX_PER_FILE]
        extra.sort(key=lambda kv: (-len(kv[1]), kv[0][1]))
        need = MIN_TAB_SENTENCES - n_tab
        for k, v in extra[:need]:
            # 挤掉一个不含 tab 且该层已有多句的
            for i in range(len(chosen) - 1, -1, -1):
                ck, cv = chosen[i]
                same_layer = sum(1 for x, _ in chosen if x[0] == ck[0])
                if not has_tab(cv) and same_layer > 1:
                    chosen.pop(i)
                    break
            chosen.append((k, v))
            used_files[k[1]] += 1

    # 按层排序输出，读起来有条理
    order = list(PER_LAYER)
    chosen.sort(key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order else 99,
                                kv[0][1]))
    global MISSING_NEEDLES
    MISSING_NEEDLES = missing_needles
    return chosen


# 「X 还原成 Y」与「`X`→`Y`」是同一种承诺，两种写法都要检。
ARROW = re.compile(r"`([^`]+)`\s*(?:→|->|还原成)\s*`([^`]+)`")


def unkept_promises(chosen: list[tuple]) -> list[str]:
    """说明文字里 `X`→`Y` 形式的例子，必须能在小样里找到对应的一行。

    只检带箭头的：那是在向老师承诺「表里能看到这条还原」。不带箭头的术语
    （`переговоры`、总统令里的词）是**外部引证**，不要求出现在这 15 句里。
    """
    pairs: set[tuple[str, str]] = set()
    for q in QUESTIONS:
        pairs |= {(a.strip(), b.strip()) for a, b in ARROW.findall(q)}
    have: set[tuple[str, str]] = set()
    for _, v in chosen:
        for r in v:
            have.add((str(r.get("term_tgt_span", "")), str(r.get("term_tgt", ""))))
            have.add((str(r.get("term_src_span", "")), str(r.get("term_src", ""))))
    return [f"`{a}`→`{b}`" for a, b in sorted(pairs) if (a, b) not in have]


def unseen_mentions(chosen: list[tuple]) -> list[str]:
    """文案里点名、但小样表里找不到的术语。**只是提醒，不是错误。**

    文案里有两类**不该**出现在表里的东西，所以不能做断言：
      * 引证 —— 词典里的单数形（`une nouille`）、词典标「无单数形」的词；
      * 故意写错的对照形（`puissance étranger`、`institución sanitario`）。
    但要老师**核对交付值**的那些（`X 还原成 Y`、`X 不压成单数`）必须在表里看得到，
    否则她只能凭信任点头。生成时看一眼这个清单。
    """
    tab: set[str] = set()
    for _, v in chosen:
        for r in v:
            for k in ("term_src", "term_tgt", "term_src_span", "term_tgt_span"):
                tab.add(str(r.get(k, "")))
    out: list[str] = []
    for q in QUESTIONS:
        for m in re.findall(r"`([^`]+)`", q):
            m = m.strip()
            if not m or m in tab or m in out:
                continue
            if any("\u4e00" <= c <= "\u9fff" for c in m):
                continue          # 中文侧不在这个检查范围
            out.append(m)
    return out


def label_sides(src: str, tgt: str) -> tuple[str, str]:
    """判定哪边是中文，返回 (src 的语言, tgt 的语言)。"""
    foreign = LANG_NAME.get(LANG, LANG)
    if cjk_ratio(src) > cjk_ratio(tgt):
        return "中文", foreign
    return foreign, "中文"


def term_cells(t: dict, src_lang: str) -> tuple[str, str, str]:
    """返回 (中文术语, 外语术语=交付值, 外语术语的句中形式)。

    俄语的交付值是词典形（一格），与句中形式不同，老师要并排核对，所以第三个
    值单独给出。`*_span` 是本轮新加的字段；旧 dump 没有就回落到交付值本身。
    """
    if src_lang == "中文":
        zh, fo = t["term_src"], t["term_tgt"]
        span = t.get("term_tgt_span") or fo
    else:
        zh, fo = t["term_tgt"], t["term_src"]
        span = t.get("term_src_span") or fo
    return zh, fo, span


def build_xlsx(chosen: list[tuple], out: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "小样"

    # 三语都多一列「句中形式」：交付值是词典形，老师要并排核对还原对不对。
    # （2026-09-18 起西/法也收词典形，不再是俄语专属。）
    show_span = SHOW_SPAN
    NCOL = 11 if show_span else 9

    def banner(text: str, *, bold=False, size=11, height=None):
        """整行横跨表宽的说明行。**必须合并单元格** —— 只写 A 列再 wrap，
        文字会挤在宽度 4 的 A 列里一个字一行，把行高撑到几百像素（踩过）。"""
        ws.append([text])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOL)
        c = ws.cell(r, 1)
        c.font = Font(bold=bold, size=size, name=BODY_FONT)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[r].height = height or (size + 6)
        return r

    # 先数一遍「已还原」的条数，好写进表头提示（渲染时才知道就晚了）。
    # 与 md 共用 count_restored()，两份说明必须报同一个数。
    n_diff = count_restored(chosen)

    banner(f"{LANG_NAME.get(LANG, LANG)}术语抽取 · 小样（请老师过目）",
           bold=True, size=14, height=24)
    banner(f"模型 {MODEL_LABEL} ｜ 共 {len(chosen)} 句、"
           f"{sum(len(v) for _, v in chosen)} 条术语 ｜ 预计 3~5 分钟")
    if QUESTIONS:
        # ⚠ 这一句以前硬编码成「需要您定」，不看 QUESTIONS_MODE —— 而 md 那侧是
        #   按 mode 分支的。结果法语那一份：开场白说「需要您定」，紧跟着的三行却说
        #   「您上次已首肯 / 已同意 / 已纠正，请复核」，第一句就和后面自相矛盾。
        #   **老师打开的是 xlsx，不是 md**，这一句是她看到的第一行字。
        if QUESTIONS_MODE == "verify":
            banner("请在最右两列直接标注。另有几处口径请您复核（都是您上次已定或"
                   "已纠正的，不是新问题），写在下面 —— 往下滚动即可，"
                   "它们不会固定在屏幕上。")
        else:
            banner("请在最右两列直接标注。另有几个口径需要您定，写在下面 —— "
                   "往下滚动即可，它们不会固定在屏幕上。")
        for q in QUESTIONS:
            # 行高按文本长度估：合并宽度约 181 字符位，一个汉字占 2 位。
            # 宁可留高也别夹断 —— 老师看不到后半句就等于没问。
            # 去掉 markdown 标记：表格里出现 ** 和反引号很突兀
            q_plain = re.sub(r"[*`]", "", q)
            lines = max(2, -(-len(q_plain) * 2 // 181))
            banner(q_plain, bold=True, height=15 * lines + 8)
    else:
        banner("请在最右两列直接标注。只要这一份看着方向对，我们就跑全量 "
               f"{LANG_FILES.get(LANG, '?')} 个文件。")
    if show_span and n_diff:
        banner(f"橙色标出、「已还原」列写「是」的共 {n_diff} 条，"
               "就是我们按规范做了还原的条目（词典形 ≠ 句中形式），请重点看这些；"
               "「已还原」列可以直接筛选。")
    ws.append([])

    if show_span:
        head = ["#", "场景", "语言", "原句", "抽出的术语",
                f"{LANG_NAME.get(LANG, LANG)}术语（词典形）", "句中形式",
                "已还原", "类型", "您的判断", "备注"]
        ask_cols = (10, 11)
        diff_cols = (6, 7, 8)      # 词典形 / 句中形式 / 已还原
    else:
        head = ["#", "场景", "语言", "原句", "抽出的术语", "对应译法", "类型",
                "您的判断", "备注"]
        ask_cols = (8, 9)
        diff_cols = ()
    ws.append(head)
    hr = ws.max_row
    for c in range(1, len(head) + 1):
        ws.cell(hr, c).font = HDR
        ws.cell(hr, c).fill = FILL_HDR
        ws.cell(hr, c).border = BOX
    for c in ask_cols:
        ws.cell(hr, c).fill = FILL_ASK

    n = 0
    for (layer, fname, sid), terms in chosen:
        n += 1
        s = terms[0]
        src_lang, tgt_lang = label_sides(s["src"], s["tgt"])
        # 两行原句：src 一行、tgt 一行
        for lang, text in ((src_lang, s["src"]), (tgt_lang, s["tgt"])):
            row = [n if lang == src_lang else "", layer if lang == src_lang else "",
                   lang, text] + [""] * (len(head) - 4)
            ws.append(row)
            r = ws.max_row
            for c in range(1, len(head) + 1):
                ws.cell(r, c).fill = FILL_SENT
                ws.cell(r, c).border = BOX
                ws.cell(r, c).alignment = WRAP
                ws.cell(r, c).font = BODY
        # 术语行
        for t in terms:
            zh, fo, span = term_cells(t, src_lang)
            changed = bool(show_span) and fo != span
            if show_span:
                ws.append(["", "", "", "", zh, fo, span,
                           "是" if changed else "", t["types"], "", ""])
            else:
                ws.append(["", "", "", "", zh, fo, t["types"], "", ""])
            r = ws.max_row
            for c in range(1, len(head) + 1):
                ws.cell(r, c).border = BOX
                ws.cell(r, c).alignment = WRAP
                ws.cell(r, c).font = BODY
            for c in ask_cols:
                ws.cell(r, c).fill = FILL_ASK
            if changed:
                for c in diff_cols:
                    ws.cell(r, c).fill = FILL_DIFF
                    ws.cell(r, c).font = DIFF_FONT

    if show_span:
        widths = {1: 4, 2: 11, 3: 6, 4: 50, 5: 18, 6: 25, 7: 25, 8: 7,
                  9: 13, 10: 16, 11: 20}
    else:
        widths = {1: 4, 2: 11, 3: 6, 4: 60, 5: 22, 6: 26, 7: 14, 8: 16, 9: 22}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    # ⚠ **不冻结任何窗格**。表头上面就是那几段问题，一冻结它们会跟着钉在屏幕顶端，
    #   往下滚也甩不掉（用户 2026-09-18 明确要求「往下滚轮滑动的时候不要固定」）。
    #   冻结只能整行整列地冻，没法只冻表头而不冻它上面的说明行，所以干脆不冻。
    ws.freeze_panes = None
    # 「已还原」列开筛选，老师可以一键只看动过的条目
    if show_span:
        ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(head))}{ws.max_row}"

    # 只有这一页。类型定义、待确认口径都**不另开 sheet** —— 老师熟悉这十个类型，
    # 口径就写在表头下面（不冻结，滚过去就没了，不占视野）。
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def count_restored(chosen: list[tuple]) -> int:
    """「已还原」的条数（词典形 ≠ 句中形式）。xlsx 与 md 共用同一个数。

    ⚠ 两份说明文字必须报同一个数：老师上一轮就是对着 xlsx 里的这个数回
      「均无误」的。分两处各数一遍，早晚会对不上。
    """
    if not SHOW_SPAN:
        return 0
    n = 0
    for _k, terms in chosen:
        sl, _tl = label_sides(terms[0]["src"], terms[0]["tgt"])
        for t in terms:
            _zh, fo, span = term_cells(t, sl)
            if fo != span:
                n += 1
    return n


def build_md(chosen: list[tuple], out: Path) -> Path:
    L = [f"# {LANG_NAME.get(LANG, LANG)}术语抽取 · 小样（请老师过目）", ""]
    L.append(f"模型 gemini-3.7-flash（高推理档）。共 {len(chosen)} 句、"
             f"{sum(len(v) for _, v in chosen)} 条术语，预计 3~5 分钟看完。")
    L.append("")
    if QUESTIONS:
        L.append("**先看这一份抽得对不对**；哪几条该删、哪几条类型该改，直接圈出来即可。")
        L.append("")
        # ⚠ 与 xlsx 的 A7 同口径、同一个数。以前这句只在 xlsx 里有，md 没有 ——
        #   而她上一轮正是对着这句回「=同意，均无误」的（那份是 7 条）。
        _nd = count_restored(chosen)
        if _nd:
            L.append(f"其中**标了 ⟵ 已还原 的共 {_nd} 条**（词典形 ≠ 句中形式），"
                     "就是我们按规范做了还原的条目，**请重点看这些**。")
            L.append("")
        if QUESTIONS_MODE == "verify":
            L.append(f"另外有 {len(QUESTIONS)} 处口径请您**复核**（都是您上次已定或"
                     "已纠正的，不是新问题），列在文末「请您复核的几处」一节 —— "
                     "先看样本，再看问题。")
        else:
            L.append(f"另外有 {len(QUESTIONS)} 个口径需要您拍板，列在文末"
                     "「需要您定的几件事」一节 —— 先看样本，再看问题。")
        L.append("")
    else:
        L.append("**只需回答一个问题：这个方向对不对？** 对就跑全量 532 个文件；"
                 "哪几条该删、哪几条类型该改，直接圈出来即可。")
        L.append("")
    if SHOW_SPAN:
        L.append(f"{LANG_NAME.get(LANG, LANG)}术语列交的是**词典形**"
                 + ("（还原到一格 + 单数）" if LANG == "ru" else "（还原到单数）")
                 + "，右边「句中形式」是它在原句里的样子。"
                 "模型必须先给出能在原句里逐字定位的切片，通过之后才由程序核对还原"
                 "（词数、功能词、专名、词元、不得复数化、性别标记不许翻转"
                 + ("，俄语再加形态分析器判主格" if LANG == "ru" else "")
                 + "），核对不过的会保留句中形式并记账，**不会丢术语**。"
                 "中文侧仍是原句切片。")
    else:
        L.append("每个术语都保证能在原句里逐字找到 —— 不是模型自己译的，是从原句切出来的。")
    L.append("")
    L.append("---")
    L.append("")
    n = 0
    for (layer, fname, sid), terms in chosen:
        n += 1
        s = terms[0]
        src_lang, tgt_lang = label_sides(s["src"], s["tgt"])
        L.append(f"### {n}. {layer}")
        L.append("")
        L.append(f"- **{src_lang}**：{s['src']}")
        L.append(f"- **{tgt_lang}**：{s['tgt']}")
        L.append("")
        fname_lang = LANG_NAME.get(LANG, LANG)
        if SHOW_SPAN:
            L.append(f"| 中文 | {fname_lang}（词典形） | 句中形式 | 类型 |")
            L.append("|---|---|---|---|")
        else:
            L.append(f"| 中文 | {fname_lang} | 类型 |")
            L.append("|---|---|---|")
        for t in terms:
            zh, fo, span = term_cells(t, src_lang)
            if SHOW_SPAN:
                # 还原过的加粗并加 ⟵ 标注：md 没有底色，只能靠符号区分
                if fo != span:
                    L.append(f"| {zh} | **{fo}** | {span} ⟵ 已还原 | `{t['types']}` |")
                else:
                    L.append(f"| {zh} | {fo} | {span} | `{t['types']}` |")
            else:
                L.append(f"| {zh} | {fo} | `{t['types']}` |")
        L.append("")
    L.append("---")
    L.append("")
    if QUESTIONS:
        L.append("## 请您复核的几处" if QUESTIONS_MODE == "verify"
                 else "## 需要您定的几件事")
        L.append("")
        for i, q in enumerate(QUESTIONS, 1):
            L.append(f"{i}. {q}")
            L.append("")
        L.append("---")
        L.append("")
    L.append("## 十个类型")
    L.append("")
    L.append("| 类型 | 含义 | 判定要点 |")
    L.append("|---|---|---|")
    for t, name, crit in [
        ("hot", "热词/新词", "近年出现或热度上升、尚未固化"),
        ("cul", "文化", "强依赖一方文化背景，对方无完全对等概念"),
        ("tech", "科技", "概念明确的技术；泛表述不算"),
        ("poli", "时政", "国家治理、外交、公共政策"),
        ("econ", "经济/金融", "宏观经济、金融、市场"),
        ("tab", "禁忌/敏感", "只标「需要注意」，不评对错"),
        ("per", "人名", "具体个人；职务不算"),
        ("loc", "地名", "地理实体；抽象集合不算"),
        ("org", "机构名", "正式组织；政策制度不算"),
        ("other", "其他", "归不进上面九类，且不与其他类并用"),
    ]:
        L.append(f"| `{t}` | {name} | {crit} |")
    L.append("")
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def main(argv=None) -> int:
    global LANG, PER_LAYER, SOURCE, MODEL_LABEL, MAX_PER_FILE, MUST_INCLUDE, QUESTIONS, QUESTIONS_MODE
    global SHOW_SPAN
    ap = argparse.ArgumentParser(prog="getterms.small_sample")
    ap.add_argument("--lang", default="es")
    ap.add_argument("--source", default=None,
                    help="bakeoff/ 下的 terms_*.json，默认用西语那份 gemini high 的结果")
    ap.add_argument("--model-label", default=None,
                    help="写在小样标题里的模型说明，默认从 --source 的文件名推")
    ap.add_argument("--no-md", action="store_true",
                    help="只出 xlsx，不生成 md（老师直接在表里打判断时用）")
    a = ap.parse_args(argv)
    LANG = a.lang
    PER_LAYER = PER_LAYER_BY_LANG.get(a.lang) or PER_LAYER_BY_LANG["es"]
    MAX_PER_FILE = MAX_PER_FILE_BY_LANG.get(a.lang, 1)
    MUST_INCLUDE = MUST_INCLUDE_BY_LANG.get(a.lang, [])
    QUESTIONS = QUESTIONS_BY_LANG.get(a.lang, [])
    QUESTIONS_MODE = QUESTIONS_MODE_BY_LANG.get(a.lang, "decide")
    SHOW_SPAN = a.lang in DICT_FORM_LANGS
    # ⚠ 默认值必须跟着语种走，而且要和其余工具同源（config.FINAL_DUMPS）。
    #   以前默认是一个无版本后缀的西语 v0 基线，跑 --lang ru 会静默出一份西语内容。
    # ⚠ FINAL_DUMPS 的值是 **Path**（绝对路径），不是文件名字符串。
    #   本模块的 SOURCE 一路当字符串用（`_label_from_source` 会 removeprefix），
    #   所以这里取 `.name`。引用别处的名字必须先确认它的类型 —— 本轮第二次栽在这上面
    #   （前一次是 FULL_CORPUS 的值其实是 (语种名, 句数) 元组）。
    _fd = FINAL_DUMPS.get(a.lang)
    SOURCE = a.source or (_fd.name if hasattr(_fd, "name") else str(_fd or ""))
    if not SOURCE:
        raise SystemExit(f"config.FINAL_DUMPS 里没有 {a.lang!r} 的定稿 dump，"
                         "请显式给 --source。")
    MODEL_LABEL = a.model_label or _label_from_source(SOURCE)

    # ⚠ 再核一遍 dump 的语种身份：层名三语相同，光看层名分不出语种。
    #   判据用文件名里的语种标记（`__fr__` / `_ru_` / 文件名含 zh-fr 之类）——
    #   `es` 那份历史上没有语种后缀，所以只在 dump 名含别的语种标记时才报错。
    _other = {"es": ("__fr", "__ru", "_fr_", "_ru_"),
              "fr": ("__ru", "_ru_", "__es", "_es_"),
              "ru": ("__fr", "_fr_", "__es", "_es_")}.get(a.lang, ())
    _sname = str(SOURCE)
    if any(k in _sname for k in _other):
        raise SystemExit(
            f"--lang {a.lang} 但 dump 名像是别的语种：{_sname}\n"
            "  三语的层名相同，所以层名对得上**不代表**语种对得上 —— "
            "这一步就是防「静默产出另一个语种的小样」。")

    by_sent = load_sentences()
    chosen = pick(by_sent)
    if not chosen:
        raise SystemExit(
            f"一句都没挑出来。检查 {SOURCE} 里的 layer 值是否与 "
            f"PER_LAYER_BY_LANG[{a.lang!r}] 的层名对得上：{sorted(PER_LAYER)}")
    n_terms = sum(len(v) for _, v in chosen)
    name = f"小样_{LANG_NAME.get(LANG, LANG)}_请老师过目"
    x = build_xlsx(chosen, OUT_DIR / f"{name}.xlsx")
    m = None if a.no_md else build_md(chosen, OUT_DIR / f"{name}.md")
    print(f"小样：{len(chosen)} 句、{n_terms} 条术语")
    layers = collections.Counter(k[0] for k, _ in chosen)
    print("分层:", dict(sorted(layers.items())))
    dirs = collections.Counter(
        label_sides(v[0]["src"], v[0]["tgt"])[0] for _, v in chosen)
    print("src 槽位语言:", dict(dirs))
    tabs = sum(1 for _, v in chosen if any("tab" in r["types"] for r in v))
    print(f"含 tab 敏感词的句子: {tabs}")
    unseen = unseen_mentions(chosen)
    if unseen:
        print("")
        print(f"提示：文案里点名、表里没有的术语 {len(unseen)} 个（引证与故意写错的对照形"
              f"属正常，其余请确认老师能否核对）：{unseen}")
    broken = unkept_promises(chosen)
    if broken:
        print("")
        print(f"⚠ 说明文字里有 {len(broken)} 条 `X`→`Y` 例子在小样里**找不到对应行**："
              f"{broken}")
        print("  老师会照着说明去表里找证据。要么换成本批真实存在的例子，"
              "要么把那条例子加进 MUST_INCLUDE。")
    if MISSING_NEEDLES:
        # 不是致命错（小样本身可用），但必须看见：意味着「我以为送审了、其实没送」
        print("")
        print(f"⚠ MUST_INCLUDE 有 {len(MISSING_NEEDLES)} 条在本 dump 里 0 命中，"
              f"**没有进小样**：{MISSING_NEEDLES}")
        print("  换语料/换 dump 之后最容易出现。要么该 needle 已失效（删掉），"
              "要么这一版真的没抽到它（是个抽取问题）。")
    print(f"\nxlsx（可直接在里面打判断）: {x}")
    if m:
        print(f"md  （方便微信/飞书里读）: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
