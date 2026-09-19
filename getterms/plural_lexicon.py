"""术语里**惯用复数**的词条清单（三语），以及判定辅助。

为什么要一份清单而不是一条规则：这是**词汇知识**，程序推不出来。
`precipitación` / `objectif` / `nouille` / `осадок` 的单数在语法上全部合法存在，
所以任何「单数存在就可以压单数」的规则都会把它们压错 —— 2026-09-18 实测三语全中，
根因是三个提示词把 IATE 的 "habitually used in the plural" 写成了
"only exists in the plural"（见 `计划` §1.1）。

这份清单有两个用处：
  1. 三个提示词「保留复数」那一条的**例子来源**（例子必须来自实证，不许编）；
  2. `selfcheck` 里「复数→单数候选」清单的**排序依据** —— 命中清单的排最前。

⚠ **它是数据不是代码。** 语言老师加词不需要改提示词、不换哈希、不用重跑全量。
⚠ 它**不完备**，也不假装完备。`source` 字段记明每条的出处，便于日后判断该不该信。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    head: str           # 复数形式的**中心词**，用于匹配（`precipitaciones`）
    term: str           # 完整术语示例（`precipitaciones atmosféricas`）
    zh: str             # 中文对应，便于老师核
    why: str            # 为什么惯用复数；「压单数即换词」的要写明
    source: str         # 出处：实证 / 老师 / 规范


# 出处标记。`source` 字段的规矩：**要么是可点开的 URL，要么明写「未独立查证」** ——
# 「我看见过」不算出处（旧值 `2026-09-18 三语 bake-off 实证` 就是这个毛病）。
OBS = "2026-09-18 三语 bake-off 实证"
UN = "联合国官方名称（法国政府 agenda-2030.fr、法语维基、UN Global Compact FR）"
DLE = "https://dle.rae.es"      # 西班牙皇家学院词典；复数标记体系是西语这侧最可核的判据


HABITUAL_PLURAL: dict[str, tuple[Entry, ...]] = {
    # ------------------------------------------------------------------ 西语
    # 2026-09-18 联网查证：DLE 的复数标记体系（`m. pl.` / `f. pl.` / `U. m. en pl.`）
    # 是这一侧最可核的判据。**删掉 `precipitaciones`** —— IATE 45260 与 AEMET 术语手册
    # 都写单数 `precipitación atmosférica`，DLE 气象义也没有复数标记。
    # 那是我自己写进 es_v7 的错示例，es_v8 已改。法语那侧相反（Larousse 另立复数词条）——
    # **跨语种类推不可靠，这就是活例子。**
    "es": (
        Entry("elecciones", "elecciones libres e imparciales", "自由公正的选举",
              "DLE 的「选举」义（义 4）**只立在复数词形下，标 `f. pl.`**；"
              "**单数 elección 首义是「选择」** —— 压单数即换词。"
              "联合国 OHCHR 西语文本亦固定用复数",
              DLE + "/elecci%C3%B3n"),
        Entry("recursos", "recursos hídricos", "水资源",
              "**IATE 条目 49761 的词条形即复数**；DLE「资源」义（义 6/7）标 `m. pl.`；"
              "**单数 recurso 首义是「求助」、义 5 是法律「上诉」** —— 压单数即换词",
              "https://iate.europa.eu/entry/result/49761/es-en"),
        Entry("masas", "masas populares", "人民群众",
              "DLE 义 9（人群）标 **`U. m. en pl.`**，例句原文即「Las masas populares」；"
              "**单数 masa 首义是物理「质量」** —— 压单数即换词",
              DLE + "/masa"),
        Entry("pueblos", "pueblos originarios", "原住民",
              "拉美官方与联合国文本固定用复数（阿根廷 INAI「los 34 pueblos originarios」、"
              "UNDRIP 西语标题「…los derechos de los pueblos indígenas」）；"
              "**单数 pueblo 首义是「村镇」（Ciudad o villa）** —— 压单数即换指称。"
              "⚠ 分情况：IATE 879135 对「单一民族」义给的是单数 `pueblo indígena`",
              "https://www.argentina.gob.ar/derechoshumanos/inai"),
        Entry("tiempos", "tiempos remotos", "远古时期",
              "DLE 把副词短语 **`en tiempos`**（=En época pasada）以复数立目；"
              "**单数 tiempo 首义是「时长」，且 `en tiempo remoto` 这个短语不成立** "
              "—— 压单数即换指称。"
              "⚠ 它更像固定短语而非名词术语条目，术语库无对应条目",
              DLE + "/tiempo"),
        Entry("palillos", "palillos", "筷子",
              "DLE 义 12「筷子」**只立在复数词形下，标 `m. pl.`**；"
              "**单数 palillo 是「织针」（义 1）/「牙签」（义 2），完全没有筷子义** "
              "—— 压单数即换词。清单里最硬的一条",
              DLE + "/palillo"),
        Entry("flores", "flores y pájaros", "花鸟",
              "泛指题材类别的**并列枚举**，西语惯用复数；压成 `flor y pájaro` 会被读成"
              "「一朵花和一只鸟」（特指）—— 压单数即换指称。"
              "⚠ 画科名亦为复数（`pintura de flores y pájaros`），但只有百科级出处。"
              "⚠ 旧版这条的理由写「作画科名称时惯用复数」，"
              "而实测原句 `se pintan paisajes, flores y pájaros` 是普通并列宾语枚举，"
              "**结论对、依据错**，已重写",
              "2026-09-18 实证 + 西语并列泛指惯例（无术语库条目）"),
        Entry("derechos", "derechos humanos", "人权",
              "**DLE 直接以 `derechos humanos` = `1. m. pl.` 立目**；IATE 条目 127688；"
              "**单数 derecho 的首义是形容词「直的」**，人权义在第 11 义"
              "（DLE 该义的例句恰好就是 Los derechos humanos）—— 压单数即换词",
              "https://iate.europa.eu/entry/result/127688/all"),
        Entry("fuerzas", "fuerzas armadas", "武装力量",
              "**DLE 直接以 `fuerzas armadas` = `1. f. pl.` 立目**；IATE 条目 843879；"
              "**单数 fuerza 首义是「力气」**，军事义是第 16 义且本身标 `f. pl.` "
              "—— 压单数即换词",
              "https://iate.europa.eu/entry/result/843879/all"),
        Entry("Objetivos", "Objetivos de Desarrollo Sostenible", "可持续发展目标",
              "联合国官方框架名（17 项作一组），名称本身是复数；"
              "单数只用于指某一项目标",
              "https://www.un.org/sustainabledevelopment/es/objetivos-de-desarrollo-sostenible/"),
        Entry("ingresos", "ingresos fiscales", "税收收入",
              "**IATE 条目 1551315 的西语词条形即复数**；"
              "**单数 ingreso 首义是「进入的动作」，财政义排到第 5 义** —— 压单数即换词。"
              "⚠ 选词提示（不影响数）：西语官方统计口径更常用 `ingresos tributarios`"
              "（AEAT 月报、CEPAL/OCDE/CIAT 联合报告），而 IATE 收的是 `ingresos fiscales`；"
              "这属于选词，归下游 term_qc",
              "https://iate.europa.eu/entry/result/1551315/all"),
        # ⚠ 这里曾有 `gastos públicos` 与 `infraestructuras` 两条，2026-09-18 查实**都该单数**、
        #   已删（它们当初的理由是「法语同类，预防性收入」——纯类推，正是被禁止的那种推法）：
        #     · DLE 的 `gasto público` 是单数子词条且不标复数，而同页 `gasto social` 标
        #       `U. t. en pl.`、`gastos de representación` 以 `m. pl.` 立目 —— 该标会标，不标即反证；
        #       IATE 搜 `gasto público` 的 28 个西语词条里裸复数 0 个。
        #     · IATE 1492661 的词条形是 `infraestructura`；全库 304 个西语 term 里
        #       裸 `infraestructuras` **0 命中**，复数只出现在带限定语的组合里。
        #   法语侧的 `infrastructures` **不跟着删** —— 那条有法国国民议会领域枚举句原文支撑，
        #   而我们法语语料那一句正是同型枚举句。证据强度不同、结论就可以不同。
    ),
    # ------------------------------------------------------------------ 法语
    # 2026-09-18 联网查证：6 条保留复数全部有硬出处；`sources de Baotu` 与
    # `vêtements chinois` 查实该用**单数**，已删（模型交的单数是对的，是清单错了）。
    "fr": (
        Entry("précipitations", "précipitations atmosphériques", "大气降水",
              "气象义惯用复数：Larousse 另立独立词条 `précipitations, n. f. pl.`，"
              "Usito 标「（surtout au plur.）météor.」；"
              "**单数 précipitation 首义是「匆忙」** —— 压单数即换词",
              "https://www.larousse.fr/dictionnaires/francais/pr%C3%A9cipitation"),
        Entry("objectifs", "objectifs de développement durable", "可持续发展目标",
              "联合国官方框架名（ODD，17 项作一组）；单数只用于指某一项目标（l'objectif n°4）",
              "https://www.un.org/fr/exhibit/odd-17-objectifs-pour-transformer-notre-monde"),
        Entry("nouilles", "nouilles instantanées", "方便面",
              "法兰西学院词典 9e 义 1 明标「**Le plus souvent au pluriel**」，"
              "6/7/8 版直接以 `nouilles, n. f. pl.` 立目；"
              "**单数 une nouille 义 2 是「笨蛋」**（Personne gauche et sotte）—— 压单数即换词",
              "https://www.dictionnaire-academie.fr/article/A9N0722"),
        Entry("vermicelles", "vermicelles Luosifen", "螺蛳粉",
              "法兰西学院 9e：复数才指粉丝实体，**单数被专门标注为集体量**"
              "（«Au singulier, avec un sens collectif»：un bouillon au vermicelle）"
              " —— 压单数即换指称",
              "https://www.dictionnaire-academie.fr/article/A9V0489"),
        Entry("dépenses", "dépenses publiques", "公共支出",
              "魁北克 GDT 的首选词形即复数（定义为整体义）；同构 finances publiques",
              "https://vitrinelinguistique.oqlf.gouv.qc.ca/fiche-gdt/fiche/507839/depenses-publiques"),
        Entry("recettes", "recettes fiscales", "税收收入",
              "GDT 明文注「*recette fiscale* et *recette d'impôt* "
              "**s'emploient le plus souvent au pluriel**」；"
              "**单数 recette 常义是「食谱」，且 Larousse 的 `recette fiscale` 单数指「税务所」**"
              " —— 压单数即换词",
              "https://vitrinelinguistique.oqlf.gouv.qc.ca/fiche-gdt/fiche/8873036/recette-fiscale"),
        Entry("infrastructures", "infrastructures", "基础设施",
              "⚠ **分情况，不是无条件复数**：GDT 立的是**单数**词条（定义即整体义），"
              "但「教育、卫生、农业、基建」这类**领域枚举**句法国官方惯用复数"
              "（国民议会书面问答原句如此）。单指一项设施用单数 une infrastructure。",
              "https://vitrinelinguistique.oqlf.gouv.qc.ca/fiche-gdt/fiche/26515201/infrastructure"),
        Entry("droits", "droits de l'homme", "人权",
              "固定多词术语；《世界人权宣言》法文标题即 Déclaration universelle des "
              "**droits** de l'homme",
              "https://www.un.org/fr/about-us/universal-declaration-of-human-rights"),
        Entry("arts", "arts et métiers traditionnels", "传统工艺",
              "并列套语（提示词自 fr_v1 起沿用）。⚠ 未独立查证，沿用历史口径",
              "fr_v1 起（未独立查证）"),
        Entry("temps", "temps anciens", "远古时期",
              "时间套语惯用复数（同 les temps modernes）；实测 fr_v5 下不稳"
              "（三次给出单数/复数/漏抽），fr_v6 起 3/3 一致给复数",
              "2026-09-18 重复采样实证（见 README「三语同一处根因」）"),
    ),
    # ------------------------------------------------------------------ 俄语
    # 2026-09-18 联网查证：3 条待判全部支持保留复数；另判定 7 条「压单数即换义」
    # （原先 ru 一条标记都没有，`meaning_shift("ru")` 恒为空）。
    # 两条**弱条目**已如实标注 —— 它们的单数也是规范形式，别当铁证用。
    "ru": (
        Entry("осадки", "атмосферные осадки", "大气降水",
              "МАС 第 4 义标「мн. ч. (осадки, -ов) Атмосферная влага…」，"
              "乌沙科夫词典标「**только мн.**」；"
              "**单数 осадок 首义是「沉淀物/渣」** —— 压单数即换词",
              "https://gramota.ru/meta/osadok"),
        Entry("рынки", "рынки капитала", "资本市场",
              "⚠ **弱条目**：单数 `рынок капитала` 本身就是标准经济学术语，压单数**不换义**。"
              "复数多指多国/多类市场。留在清单里只因 es/fr 同概念惯用复数，"
              "不足以当硬判据。",
              "https://kartaslov.ru/значение-слова/рынок（弱，单数亦规范）"),
        Entry("интересы", "национальные интересы", "国家利益",
              "总统令第 400 号（2021）第 5 条把「национальные интересы РФ」列为定义术语，"
              "第 683 号（2015）与 2025 年民族政策战略同样只用复数，**官方文本零单数用例**；"
              "МАС「利益」义标「обычно мн. ч.」、乌沙科夫标「только мн.」，"
              "而**单数 интерес 首义是「兴趣、好奇」** —— 压单数即换词",
              "http://www.kremlin.ru/acts/bank/47046"),
        Entry("места", "рабочие места", "就业岗位",
              "⚠ **弱条目**：劳动法典第 209 条**以单数定义**该术语（рабочее место），"
              "单数即规范形式，压单数**不换义**。就业统计语境用复数是行业惯例。",
              "https://www.consultant.ru/document/cons_doc_LAW_34683/（弱，单数为法定形式）"),
        Entry("запасы", "запасы льда", "冰储量",
              "自然资源类习惯用复数；МАС 单复数皆可，压单数**不换义**",
              "https://kartaslov.ru/значение-слова/запас"),
        Entry("народы", "народы всего мира", "世界各国人民",
              "联合国俄文官方只用复数（«для всех народов мира»、«право на "
              "самоопределение народов»）；**单数 народ 首义是「某国的人口/居民」，"
              "必须带国别** —— 压单数即换指称",
              "https://www.un.org/ru/documents/decl_conv/declarations/summitdecl.shtml"),
        Entry("учения", "совместные учения", "联合演习",
              "МАС 军事演习义标「обычно мн. ч.」；"
              "**单数 учение 首义是「学说/理论体系」** —— 压单数即换词",
              "https://kartaslov.ru/значение-слова/учение"),
        Entry("права", "права человека", "人权",
              "《世界人权宣言》俄文标题即 Всеобщая декларация **прав** человека；"
              "**单数 право 的首义标「только ед. ч.」＝「法（规范总和）」** —— 压单数即换词",
              "https://www.un.org/ru/documents/decl_conv/declarations/summitdecl.shtml"),
        Entry("переговоры", "переговоры", "谈判",
              "**pluralia tantum —— 现代俄语没有单数形**，词典词条直接标 мн.。"
              "这是清单里最硬的一条；压单数即换（造不出合法单数）",
              "https://kartaslov.ru/значение-слова/переговоры"),
        Entry("доходы", "доходы бюджета", "财政收入",
              "预算法典第 6 条的**法定定义术语，原文即复数**。"
              "（单数 доход 与复数同义，所以判据是「法定术语形式」而不是换义。）",
              "https://www.consultant.ru/document/cons_doc_LAW_19702/"),
        Entry("расходы", "расходы бюджета", "财政支出",
              "预算法典第 6 条的**法定定义术语，原文即复数**；"
              "МАС「经费/支出」义标「обычно мн. ч. (расходы, -ов)」，而"
              "**单数 расход 第 2 义是「消耗量」**（расход воды/энергии）—— 压单数即换词（较弱）",
              "https://www.consultant.ru/document/cons_doc_LAW_19702/"),
    ),
}


def heads(lang: str) -> frozenset[str]:
    """该语种清单里全部「复数中心词」的 casefold 集合，供 selfcheck 排序用。"""
    return frozenset(e.head.casefold() for e in HABITUAL_PLURAL.get(lang, ()))


def lookup(lang: str, word: str) -> Entry | None:
    """按中心词查清单。命中说明这个词**惯用复数**，压成单数很可能是错的。"""
    w = word.casefold()
    for e in HABITUAL_PLURAL.get(lang, ()):
        if e.head.casefold() == w:
            return e
    return None


# 匹配时要忽略的功能词。**不能把它们算进补语交集** —— 否则 `sources de Jinan`
# 会因为共享一个 `de` 就命中 `sources de Baotu`。
_FUNC: dict[str, frozenset[str]] = {
    "es": frozenset("de del la las el los un una unos unas y e o en a al con para por".split()),
    "fr": frozenset("de des du la le les un une et ou en a au aux dans pour par".split()),
    "ru": frozenset("и в во на с со для от до по за из о об у не".split()),
}
# 法语/西语的缩合形（`d'eau`、`l'homme`）要先切掉前缀再判。
_ELIDE = frozenset("d l qu n s j c m t".split())
_STRIP = " \t .,;:!?()[]{}«»\"'’“”—–-"


def _norm_tok(tok: str, lang: str) -> str:
    n = tok.casefold().strip(_STRIP)
    if lang in ("es", "fr") and "'" in n:
        pre, _sep, rest = n.partition("'")
        if pre in _ELIDE and rest:
            n = rest.strip(_STRIP)
    if lang in ("es", "fr") and "’" in n:
        pre, _sep, rest = n.partition("’")
        if pre in _ELIDE and rest:
            n = rest.strip(_STRIP)
    return n


def content_tokens(text: str, lang: str) -> list[str]:
    """实词序列：去标点、去功能词、casefold，缩合形切前缀。**不做词元化** ——
    词元化在 `lookup_term` 里按「集合交集」做，见那里的注释。
    """
    fw = _FUNC.get(lang, frozenset())
    out: list[str] = []
    for raw in str(text or "").split():
        n = _norm_tok(raw, lang)
        if n and n not in fw:
            out.append(n)
    return out


def _keys(tok: str, norm=None) -> frozenset[str]:
    """一个词形的「身份集合」＝字面 ∪ 全部可能词元。

    ⚠ 为什么是**集合**而不是单个词元：pymorphy3 的 `parse()[0].normal_form`
      对同一词位的不同词形会给出不同的串 —— 实测 `осадков` -> `осадки`，
      而 `осадки` -> `осадка`（**另一个词**，船舶吃水）。取单值必然对不上。
      `extract._same_lexeme_ru` 早就用集合交集，这里当时没跟上。
    """
    ks = {tok}
    if norm is not None:
        try:
            ks |= {str(x).casefold() for x in norm(tok) if x}
        except Exception:
            pass
    return frozenset(ks)


def _same(a: frozenset[str], b: frozenset[str]) -> bool:
    return bool(a & b)


def lookup_term(lang: str, span: str, norm=None) -> Entry | None:
    """按**整条术语**查清单（tier1 用这个，不要用 `lookup`）。

    三级判定，从强到弱：
      ① 实词逐位同词 —— 最强；
      ② 一侧只有中心词（单词条目 `infrastructures`/`переговоры`，或切片只剩中心词）；
      ③ 中心词同词**且补语实词有同词交集** —— 挡住 `sources d'eau` 撞 `sources de Baotu`。

    中心词位置不固定（俄语 `атмосферные осадки` 的中心词在第二位），所以一律按
    「中心词在实词集合里」判，不假设它在首位。`norm` 只给俄语传（见 selfcheck.lex_norm）。
    """
    if not span:
        return None
    sc = [_keys(w, norm) for w in content_tokens(span, lang)]
    if not sc:
        return None
    for e in HABITUAL_PLURAL.get(lang, ()):
        ec = [_keys(w, norm) for w in content_tokens(e.term, lang)]
        if not ec:
            continue
        if len(sc) == len(ec) and all(_same(x, y) for x, y in zip(sc, ec)):
            return e
        eh = _keys(_norm_tok(e.head, lang), norm)
        if not any(_same(k, eh) for k in sc):
            continue
        if not any(_same(k, eh) for k in ec):
            continue
        rest_s = _drop_one(sc, eh)
        rest_e = _drop_one(ec, eh)
        if not rest_s or not rest_e:
            return e
        if any(_same(x, y) for x in rest_s for y in rest_e):
            return e
    return None


def _drop_one(seq: list[frozenset[str]], key: frozenset[str]) -> list[frozenset[str]]:
    """去掉**第一个**与 key 同词的位置，其余保留（中心词只该被消掉一次）。"""
    out, done = [], False
    for k in seq:
        if not done and _same(k, key):
            done = True
            continue
        out.append(k)
    return out


def examples_for_prompt(lang: str, limit: int = 8) -> list[str]:
    """给提示词用的例子（完整术语形式），只取实证来源的，稳定排序。"""
    es = [e for e in HABITUAL_PLURAL.get(lang, ())]
    es.sort(key=lambda e: (e.source != OBS, e.term))
    return [e.term for e in es[:limit]]


# 「压成单数就不是同一个意思了」的统一标记。清单里写「换词」或「换指称」都算，
# 判据只认这个前缀 —— 措辞可以调，标记不许漂。
MEANING_SHIFT_MARK = "压单数即换"


def meaning_shift(lang: str) -> list[Entry]:
    """「压单数即换词/换指称」的那些 —— 提示词里要单独警告。"""
    return [e for e in HABITUAL_PLURAL.get(lang, ())
            if MEANING_SHIFT_MARK in e.why]
