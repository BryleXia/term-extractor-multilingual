"""导出层：把术语行写成与 HTML 工具逐字同口径的 xlsx，并打包 results.zip。

**推理与交付分离**（计划 §1）：本模块不碰 API。所有格式决策都在 ExportConfig 里，
下游要改列名/sheet 名/文件名规则，改配置重导出即可，不需要重跑那几万次调用。
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook

from .extract import TermRow


@dataclass
class ExportConfig:
    """默认值 = 飞书 2.8 / 2.9 样例的口径。改这里就能改交付结构。

    ⚠ **9 列，两个新列追加在末尾**（2026-09-19 定稿）。前 7 列的名字与**位置**
    与 `2.8 align.term.xlsx数据结构样例` 逐字一致 —— 任何按列位置读的下游
    （`tests.py:191` 就是）都不会错位。

    为什么要多两列：`final.json` 把术语内联标在句子里（`当前[世界经济]{term}面临…`），
    转 json 时必须能在原句里**定位**到那个词，所以第 4/5 列必须是**句中原始切片**；
    而术语库按国际术语规范收**词典形**（老师定的口径），两样都得交 ——
    实测外语侧约**两成**条目的两者不同（西语全量 20.6%；法语 `chefs d'État` → `chef d'État`，
    俄语更多）。⚠ 早期版本这里写「约一半」，那是个小样本上的读数，别再用。
    """
    columns: tuple[str, ...] = (
        "sent_id", "src_text", "tgt_text", "term_src", "term_tgt", "types", "note",
        "term_src_dict", "term_tgt_dict",
    )
    sheet_name: str = "terms"
    # HTML 只写有术语的行；0 术语的文件不产出 xlsx
    skip_empty_files: bool = True
    # zip 里扁平放 basename（HTML 会带上 zip 内的 07 align_qc/ 子目录，我们不）
    flatten_in_zip: bool = True
    zip_name: str = "results.zip"
    note_always_empty: bool = True


DEFAULT_EXPORT = ExportConfig()


def write_xlsx(rows: list[TermRow], out_path: Path,
               cfg: ExportConfig = DEFAULT_EXPORT) -> bool:
    """写一个 xlsx。返回是否真的写了（0 行且 skip_empty_files 时不写）。"""
    if not rows and cfg.skip_empty_files:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = cfg.sheet_name
    ws.append(list(cfg.columns))
    for r in rows:
        d = {
            "sent_id": r.sent_id,
            "src_text": r.src_text,
            "tgt_text": r.tgt_text,
            # 第 4/5 列 = **句中原始切片**，逐字来自原句。
            # 用途：`final.json` 的内联标注 `[句中原文]{类型}` 靠它在原句里定位。
            # ⚠ 所以它**不能**是词典形、**也不能**被标点归一化动过 ——
            #   一动就落在原句之外，下游定位不到。归一化去第 8/9 列。
            "term_src": r.term_src,
            "term_tgt": r.term_tgt,
            "types": r.types,
            "note": "" if cfg.note_always_empty else r.note,
            # 第 8/9 列 = **词条形式**：有词典形就词典形，没有则与第 4/5 列相同
            # （`TermRow.out_src` 就是这个语义，没改）。老师的「术语库收词典形」
            # 口径落在这里；`extract.normalize_delivery` 的撇号 / 外层引号归一化
            # 也落在这里 —— 那正是它该在的地方。
            # 列名是暂定，交付说明里写明「列名你们定，我们照办」。
            "term_src_dict": r.out_src,
            "term_tgt_dict": r.out_tgt,
        }
        ws.append([d.get(c, "") for c in cfg.columns])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ⚠ 原子落盘（照 llm.py 缓存已有的写法）。openpyxl 直接以最终名打开目标写 zip 容器，
    #   中途 Ctrl-C / 磁盘满会留下**一个名字完全正常的坏 xlsx**，肉眼分辨不出来。
    #   532 个文件的导出阶段被打断过一次就会中招。
    tmp = out_path.with_name(out_path.name + f".tmp{os.getpid()}")
    try:
        wb.save(tmp)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return True


def pack_zip(xlsx_paths: list[Path], out_dir: Path,
             cfg: ExportConfig = DEFAULT_EXPORT,
             extra: tuple[Path, ...] = ()) -> Path | None:
    """把产出的 xlsx 打成一个 zip 交付。

    `extra` 是随包一起走的说明文件（字段说明 markdown）。**默认空**，
    所以老调用点逐字不变。放进去是因为**交付物就是这份 zip** ——
    说明不跟着数据走，对方解压出来的就只有一堆表。
    """
    if not xlsx_paths:
        return None
    zp = out_dir / cfg.zip_name
    # 原子落盘：这份 zip 是交给下游工具方的那个，不能出现半截文件。
    ztmp = zp.with_name(zp.name + f".tmp{os.getpid()}")
    try:
        with zipfile.ZipFile(ztmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in xlsx_paths:
                arc = p.name if cfg.flatten_in_zip else str(p.relative_to(out_dir))
                z.write(p, arcname=arc)
            for p in extra:
                if p.exists():
                    z.write(p, arcname=p.name)
        os.replace(ztmp, zp)
    except BaseException:
        try:
            ztmp.unlink()
        except OSError:
            pass
        raise
    return zp


README_TEMPLATE = """# 术语抽取交付说明（{lang}）

生成时间：{ts}
模型：{model}    推理档位：{effort}    批大小：{batch_size}
提示词：{prompt}（哈希 {phash}）

## 口径

- 列（9 列）：{columns}
  - 第 4/5 列 `term_src`/`term_tgt` = **句中原始切片**（逐字来自原句，未做任何归一化）
  - 第 6 列 `types` = 类型；一个术语可能属于多个类型，用英文逗号连接
  - 第 8/9 列 `term_src_dict`/`term_tgt_dict` = **词条形式**（词典形；没有词典形时与第 4/5 列相同）
- sheet 名：`{sheet}`
- 文件名规则照抄 `replaceOutName()`：`*_align.qc.json` → `*_term.xlsx`；
  `*.align.qc.json` → `*.term.xlsx`
- 只写有术语的行；0 术语的文件不产出 xlsx
- `note` 列恒空

## 与 2.8 / 2.9 样例、以及 HTML 工具的差异

⚠ **本节的「样例」一律指 `2.8 align.term.xlsx数据结构样例`。**
2.9（`term_qc`）虽然**也是 9 列**，但排布完全不同 —— 它的第 7/8/9 列是
`editor_student` / `editor_teacher` / `note`，本表是 `note` / `term_src_dict` /
`term_tgt_dict`。**两表列数相同、内容不同，千万不要拿 2.9 的表头当模板来接本表。**

1. **zip 内扁平放 basename，没有 `term/` 目录前缀**。HTML 会把上传 zip 里的
   `07 align_qc/` 子目录一起带进输出名，我们只用文件名本身。
   3.0 命名规则里写的路径是 `term/{{…}}_term.xlsx` —— 合并时请把本包的文件
   放进你们的 `term/` 目录。
2. **每个 `term_src`/`term_tgt` 都是从原句里切出来的真实子串**，不是模型回抄的
   字符串。模型给的术语若无法在原句中逐字定位，会先纠正重试一次，仍不过则丢弃
   并记录在报告里。
3. **比 2.8 样例多两列**（第 8/9 列 = 词条形式）。2.8 那 7 列的**位置原封不动**，
   新增的两列**追加在末尾** —— 按列位置读的下游不会错位。**列名你们定，我们照办。**
   （**名字**有一处不同：见下条。）
4. **第 6 列列名跟 HTML 工具用 `types`**（2.8 样例里写的是 `type`，
   2.81 那张类型词表里叫 `term_type` —— 三份材料三个名字，我们按工具口径）；
   值允许多个，逗号连接。
5. **类型词表用现行十类**（HTML 工具的 `Allowed types` 与 `2.81term_type.xlsx`
   **取值集合逐字相同** —— 只是 2.81 的行序把 `other` 排在最后，
   按整串比对会不一致，按集合比一致）：
   `other, hot, cul, tech, poli, econ, tab, per, loc, org`。
   我们的 `other` 对应 2.8 / 2.10 样例里的 `term`（通用术语）。
   **`num` 不在现行十类里** —— 它是 `2.8` 旧下拉表的值，HTML 工具与 2.81 词表都没有它。
   ⚠ 若下游按 2.8 的下拉表（`term,cul,hot,tab,per,loc,org,num`）校验标签，
   `other` / `tech` / `poli` / `econ` **一个都不在里面**，必须先按映射转一次。
   完整映射见同包的 `术语表字段说明.md`。
{lang_specific}
## 上游语料需要注意的问题

{issues}

## 本次运行统计

{stats}
"""


def write_readme(out_dir: Path, **kw) -> Path:
    # 语种专属补充说明。收词典形的语种（es/fr/ru）会填 `run.DICT_FORM_CALIBER`；
    # 其余语种留空，README 逐字节保持原样。
    kw.setdefault("lang_specific", "")
    p = out_dir / "README.md"
    p.write_text(README_TEMPLATE.format(**kw), encoding="utf-8")
    return p


# 字段说明：随交付包一起发的那份 markdown。**写给人、也给对方的 AI 读** ——
# 目标是让「xlsx → final.json」那一步照着做就行，不需要再回来问。
# 它同时进 zip（放在 `term/` 里与表同级），因为交付物是 zip，说明要跟着数据走。
FIELD_SPEC_NAME = "术语表字段说明.md"

FIELD_SPEC_TEMPLATE = """# 术语表字段说明与 final.json 映射

生成时间：{ts}
适用：术语抽取（流水线阶段 2.8），产出 `{{语向}}_{{场景}}_{{领域}}_{{会话}}_{{段号}}_term.xlsx`

⚠ **本包（zip）内是扁平放置的，没有 `term/` 目录前缀。** 3.0 命名规则里写的
`term/{{…}}_term.xlsx` 指的是它在完整数据组织里的落点 —— 合并时请把本包的文件
放进你们的 `term/` 目录。表名本身（`{{语向}}_{{场景}}_{{领域}}_{{会话}}_{{段号}}_term.xlsx`）
与 3.0 一致 —— **但有个例外**：极少数输入文件名本身就是点号式
（`…_align.qc.json` → `…_algin.qc.json` 之类，上游命名不规范），
按 `replaceOutName()` 出来会是 `….term.xlsx` 而不是 `…_term.xlsx`。
**具体哪几个见随包的 `交付说明_给下游工具方.md`**，合并前统一改名即可。

一行 = 一条术语。同一句有多条术语时，`sent_id` / `src_text` / `tgt_text` 三列**重复**出现。

⚠ **`sent_id` 有例外，合并前务必看这一条。**
语料里有少数 `align.qc.json` 的 `sent_id` 本身不规范（**全是 0**、或**有重复**）。
对这类文件我们按数组位置重新编号成 `1..N`，因此它们的 `sent_id`
**与上游原文件不一致**（`src_text` / `tgt_text` 不受影响，仍是逐字原文）。
**这批文件请按数组顺序关联（第 i 行对第 i 句），不要按 `sent_id` 关联。**
具体是哪几个文件、以及每个文件的全部上游异常，见随包附的
`交付说明_给下游工具方.md` 第三节 —— **那份和我们给您的不是同一份文件，请一并取用**。

⚠ **不要拿 2.9（`term_qc`）的表头当模板接本表。** 2.9 也是 9 列，但第 7/8/9 列是
`editor_student` / `editor_teacher` / `note`；本表是 `note` / `term_src_dict` /
`term_tgt_dict`。**列数相同、内容不同**，按位置读会全错位。

| # | 列名 | 含义 | 用途 |
|---|---|---|---|
| 1 | `sent_id` | 句号，**通常**与对应 `align.qc.json` 里 `sentences[].sent_id` 一致 —— **但有例外，见下面 ⚠** | 认回原句、取时间戳 |
| 2 | `src_text` | 原语整句 | 核对 / 定位 |
| 3 | `tgt_text` | 译语整句 | 核对 / 定位 |
| 4 | `term_src` | 术语在原语侧的**句中原始切片**（原句里怎么写就怎么抄） | 在 `src_text` 里定位 |
| 5 | `term_tgt` | 术语在译语侧的**句中原始切片** | 在 `tgt_text` 里定位 |
| 6 | `types` | 类型，多个用英文逗号连接 | 内联标注的 `{{类型}}` |
| 7 | `note` | 留空 | — |
| 8 | `term_src_dict` | 原语侧的**词条形式**（词典形；无词典形时与第 4 列相同） | 术语库的规范形式 |
| 9 | `term_tgt_dict` | 译语侧的**词条形式**（同上） | 术语库的规范形式 |

## 为什么要两套形式

有词形变化的语言（西语、法语、俄语）里，句子里出现的是变格或变数之后的形式，
不是词条的标准写法；术语库按国际术语规范收**词典形**。

| 语种 | 句中原始切片（第 4/5 列） | 词条形式（第 8/9 列） |
|---|---|---|
| 法语 | `chefs d'État` | `chef d'État` |
| 法语 | `chefs d'État et de gouvernement` | `chef d'État et de gouvernement` |
| 俄语 | `телебашен` | `телебашня` |
| 西班牙语 | `países en desarrollo` | `país en desarrollo` |
| 西班牙语 | `subvenciones estatales` | `subvención estatal` |

⚠ **表里的例子都是本批交付里真实出现的**，您可以直接在 xlsx 里搜。您那边不必照抄这两句，
它们只说明**两列的形状差别**。

⚠ **注意反例**：有些术语**习惯上就用复数**，这类我们**故意不还原**，
第 8/9 列会与第 4/5 列相同（例：`pueblos originarios`、`derechos humanos`、
`Naciones Unidas`、`Estados Unidos`）。判据是「词典/术语库里通常怎么写」，
不是「有没有单数形式」。

两者不可互相推导（要形态分析），所以**两套都交**。第 4/5 列是转 json 时定位用的，
第 8/9 列是术语库要的。中文侧没有词形变化，两列**应当**相同。

## 类型（十类）

```
other  hot  cul  tech  poli  econ  tab  per  loc  org
```

一个术语**可能属于多个类型**，表里用英文逗号连接，例如 `loc,poli`
（西语全量实测 **25.7%** 的行是多类型）。**多类型的内部顺序不作为语义**，
下游按集合处理即可。

### 与 2.8 / 2.10 样例标签的映射

三份材料三个口径，我们的十类取自 `2.81term_type.xlsx`（唯一一张正式词表）。
转 `final.json` 的内联标注时按下表对照：

| 我们的 `types` | 2.8 下拉表 | 2.10 内联样例 | 说明 |
|---|---|---|---|
| `other` | `term` | `{{term}}` | **通用/专业术语，最常见的一类** |
| `hot` `cul` `tech` `poli` `econ` `tab` `per` `loc` `org` | 同名 | 同名 | 逐字一致 |
| —（无此值） | `num` | `{{num}}` | **`num` 不在现行十类里** —— 它是 2.8 旧下拉表的值，HTML 工具与 2.81 词表都没有它 |

⚠ **两处会被追问、请先对齐**：

1. **`other` 就是你们的 `term`。** 若下游硬编码 `{{term}}`，我们的通用类术语
   **一条都标不上**。二选一：下游把 `other` 映射成 `term`，或告诉我们改用 `term`。
2. **2.8 的下拉表里没有 `other` / `tech` / `poli` / `econ`。** 若你们的校验按那张
   表走，这四个值会被判非法。**以 2.81 的词表为准**（它是正式的类型表）。

## 转 json 时怎么用（示例）

原句（译语侧）：
`Alors les concessions à l'époque étaient des zones qui étaient administrées par des puissances étrangères.`

表里对应的两行：

| sent_id | term_tgt | term_tgt_dict | types |
|---|---|---|---|
| 6 | `concessions` | `concession` | `tab` |
| 6 | `puissances étrangères` | `puissance étrangère` | `poli` |

**内联标注**用第 5 列（句中原始切片）在原句里定位：

```json
"text": "Alors les [concessions]{{tab}} à l'époque étaient des zones qui étaient administrées par des [puissances étrangères]{{poli}}."
```

**词条形式**作为术语的附加字段带出（字段名请你们定，这里只是建议）：

```json
"terms": [
  {{"src": "租界", "tgt": "concessions",
    "src_dict": "租界", "tgt_dict": "concession", "types": "tab"}},
  {{"src": "外国", "tgt": "puissances étrangères",
    "src_dict": "外国", "tgt_dict": "puissance étrangère", "types": "poli"}}
]
```

## 与 2.8 / 2.9 样例的差异

样例那 7 列的**名字与位置原封不动**，新增的两列**追加在末尾**。
所以按列位置读的下游不会错位；按列名读的多两列。

| | 2.8 / 2.9 样例 | 本表 |
|---|---|---|
| 第 4/5 列 | 句中原样 | **一致** |
| 第 6 列列名 | `type` | `types`（跟 HTML 工具，2026-09-19 拍板） |
| 第 6 列取值 | 单个 | 可多个，逗号连接 |
| 列数 | 7 | 9（新增词条形式两列） |
| 类型词表 | 含 `term`、`num` | 现行十类（`term`→`other`；`num` 不在现行十类里） |

**列名如果需要改成你们的口径，说一声即可 —— 改这一层不需要重新跑抽取。**
"""


def write_field_spec(out_dir: Path, ts: str) -> Path:
    """写交付包里的字段说明 markdown（同时会被打进 zip）。"""
    p = out_dir / FIELD_SPEC_NAME
    p.write_text(FIELD_SPEC_TEMPLATE.format(ts=ts), encoding="utf-8")
    return p
