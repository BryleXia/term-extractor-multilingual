# -*- coding: utf-8 -*-
r"""端到端：本轮新花费超过 `--max-spend` 时，run.py 真的会停下来吗？

## 为什么必须真跑一遍

**花费熔断是末段（不给 `--max-spend`）唯一的花钱上限** —— 那时靠 `_auto_budget`
（工作量外推 ×1.5）兜底。它坏掉，就没有任何东西拦得住花钱。而它此前的覆盖
**只有源码字符串断言**（`tests.py` 里 `"--max-spend" in src` 那种），
2026-09-19 的审计把它列为「最该补的一条」。

⚠ 同仓的余额熔断有 `_e2e_balance_breaker.py`，花费与 429 两条**没有** —— 不对称。
而且 `_e2e_balance_breaker.py` **自己也测不到花费熔断**：它的 argv 不带
`--max-spend`、假 client 也不产生 token，`new_spend_usd` 恒 0，那个分支永远不进。

## 做法

只把 `LLMClient` 换成「返回合法术语 + 报定量 token」的子类（复用全部真实逻辑：
记账、派发信号量、熔断、导出、闸门），然后看：

**第一轮（`--max-spend 0.005`，单批成本 $0.009）**
  退出码 3 / `stop_reason` = 花费熔断 / 越线 / **后续批被跳过**（熔断的全部意义）
  / **只发了一次调用**（停派发是真的停）/ 跳过的批进了可定位清单
  / **xlsx 照写**（熔断不阻断落盘）/ **没有 results.zip**（闸门拦下交付包）
  / 越线幅度在「并发 × 单批成本」之内（`run.py:623-625` 承诺的界）。

**第二轮（`--max-spend 10`）**
  不熔断、四批全派发、`skipped_by_budget == 0`、退出码 0、results.zip 产出。

**零 API 调用**（假 client 不发 HTTP），`cache_root` 指向临时目录。
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

# 本脚本从 `e2e/` 子目录运行，先把仓库根加进 sys.path（原来它在根目录下）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# ⚠ 本脚本**从不发真实请求**（假 client 或假 HTTP 层），但 `LLMClient.__init__`
#   会读一次 key 才肯构造。给个哑值即可 —— 环境变量优先于 key 文件，真 key 碰不到。
#   这样一来这个脚本**任何人拿到都能跑**，不需要先配一把真 key。
os.environ.setdefault("INFERERA_API_KEY", "sk-dummy-e2e-never-sends-a-request")
# 公开仓库里没有生产语料 —— 用 fixtures/ 里的**合成夹具**（内容全部虚构）。
from getterms import config as _cfg
_cfg.CORPUS_DIRS["es"] = (pathlib.Path(__file__).resolve().parent.parent
                          / "fixtures" / "corpus_es")


import getterms.run as R
from getterms.llm import CallResult, LLMClient

# ⚠ 绕过了 `run.py` 底部的 `if __name__ == "__main__": utf8_stdout()` ——
#   不显式设一次，本脚本的成败会取决于控制台编码（UTF-8 过 / GBK 挂）。
from getterms.config import utf8_stdout

utf8_stdout()

FAILED = []
MADE = []

_CJK = re.compile(r"[一-鿿]+")
_STRIP = ".,;:!?¡¿\"'()—-·«»…"

# 单批成本 = prompt/1e6*0.75 + completion/1e6*3.75（cached=0）。
# 2000 + 2000 token -> $0.0090。与 `--max-spend 0.005` 一比就知道该在哪停。
PROMPT_TOK = 2000
COMPLETION_TOK = 2000
PER_BATCH_USD = PROMPT_TOK / 1e6 * 0.75 + COMPLETION_TOK / 1e6 * 3.75


class MeasuredClient(LLMClient):
    """返回合法术语，并**报定量 token**，让 `new_spend_usd` 真的涨起来。

    为什么必须有 token：花费熔断的判据是 `rep.new_spend_usd > max_spend`，
    而它只在 `not c.from_cache` 时累加。一个不报 token 的假 client 会让这个分支
    **永远不进** —— 那正是 `_e2e_balance_breaker.py` 测不到花费熔断的原因。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tmp_cache = tempfile.mkdtemp(prefix="spend_e2e_cache_")
        self.cache_root = pathlib.Path(self._tmp_cache)
        MADE.append(self._tmp_cache)

    async def call(self, messages, file_md5, batch_idx, batch_key=""):
        user = messages[-1]["content"]
        k = user.find('"sent_id"')
        if k < 0:                       # preflight 探针
            return CallResult(ok=True, content="ok", finish_reason="stop",
                              attempts=1, model=self.price_key, effort=self.effort,
                              wire_fp=self.wire_fp)
        i, j = user.rfind("[", 0, k), user.rfind("]")
        pairs = json.loads(user[i:j + 1])
        items = []
        for p in pairs:
            src, tgt = str(p.get("src") or ""), str(p.get("tgt") or "")
            m_src, m_tgt = _CJK.search(src), _CJK.search(tgt)
            # ⚠ 语向两个方向都要认：`zh-es` 的中文在 src，`es-zh` 的中文在 tgt。
            #   第一版判反了（把 tgt 的中文当 term_src），于是 40 条全部锚定失败、
            #   0 条留下、连 xlsx 都没产出 —— 测试自己红了，不是流水线的问题。
            if m_src and not m_tgt:
                zh_text, fo_text, zh_side = src, tgt, "src"
            elif m_tgt and not m_src:
                zh_text, fo_text, zh_side = tgt, src, "tgt"
            else:
                continue
            zh = _CJK.search(zh_text).group(0)[:3]
            # 外语侧取一个**逐字子串**（剥掉首尾标点即可保证仍在原句里）。
            fo = ""
            for w in sorted(fo_text.split(), key=len, reverse=True):
                s = w.strip(_STRIP)
                if len(s) >= 5:
                    fo = s
                    break
            if not zh or not fo:
                continue
            ts, tt = (zh, fo) if zh_side == "src" else (fo, zh)
            # ⚠ `dict_form` 永远描述**外语那一侧**（extract.py:1001-1004），所以这里
            #   给 `fo` 而不是 `tt`。给成中文侧的话校验会报「引入了原文没有的汉字」，
            #   每批记一堆失败 —— 测试自己第一版就是这么错的。
            items.append({"sent_id": p["sent_id"],
                          "terms": [{"term_src": ts, "term_tgt": tt,
                                     "dict_form": fo,
                                     "types": ["other"], "note": ""}]})
        return CallResult(ok=True, content=json.dumps(items, ensure_ascii=False),
                          finish_reason="stop", attempts=1,
                          prompt_tokens=PROMPT_TOK, completion_tokens=COMPLETION_TOK,
                          model=self.price_key, effort=self.effort,
                          wire_fp=self.wire_fp)


def spent_of(rep) -> float:
    """本轮新花。

    ⚠ 在 `derived` 下面，不在顶层：`report.py:432` 只序列化 dataclass 的
    `__dict__`，而 `new_spend_usd` 是 **`@property`**，不进 `__dict__`。
    第一版直接读顶层，拿到恒为 `None`，两条断言因此假红。
    """
    return float((rep.get("derived") or {}).get("new_spend_usd") or 0.0)


def ck(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


def run_once(extra):
    """跑一轮，返回 (退出码, 报告 dict, out 目录, 这段 stdout)。目录由调用方清理。"""
    tmp = tempfile.mkdtemp(prefix="spend_e2e_out_")
    MADE.append(tmp)
    argv = [
        "--lang", "es", "--model", "gemini-3.7-flash", "--effort", "high",
        "--limit-files", "1", "--limit-sentences", "40",   # 批 10 -> 4 批
        "--out", tmp, "--tag", "spend_e2e",
        "--concurrency", "1", "--force", "--skip-preflight", "--no-correct-retry",
    ] + extra
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = R.main(argv)
    js = sorted(pathlib.Path(tmp).glob("report_*.json"))
    rep = json.loads(js[-1].read_text(encoding="utf-8")) if js else {}
    return rc, rep, pathlib.Path(tmp), buf.getvalue()


R.LLMClient = MeasuredClient            # ← 只换这一处，其余全是真的

try:
    print(f"（单批成本固定为 ${PER_BATCH_USD:.4f}）\n")

    # ================= 第一轮：熔断必须发生 =================
    CAP = 0.005
    CONC = 1
    print(f"=== 第一轮：--max-spend {CAP}（单批 ${PER_BATCH_USD:.4f}，应熔断）===")
    rc, rep, out, out_txt = run_once(["--max-spend", str(CAP)])
    ck("退出码是 3（交付包被失败批闸门拦下）", rc == 3, f"实际 {rc}")
    ck("stop_reason 是「花费熔断」", rep.get("stop_reason") == "花费熔断",
       f"stop_reason={rep.get('stop_reason')!r}")
    spent = spent_of(rep)
    ck("本轮新花真的超过了上限（判据看的就是它）", spent > CAP,
       f"new_spend={spent:.4f} vs 上限 {CAP}")
    ck("**后续批被跳过**（熔断的全部意义）", rep.get("skipped_by_budget", 0) >= 1,
       f"skipped_by_budget={rep.get('skipped_by_budget')}（共 {rep.get('batches')} 批）")
    ck("**停派发是真的停了：只发出去 1 次调用**", rep.get("calls") == 1,
       f"calls={rep.get('calls')} —— >1 说明熔断晚了一拍")
    ck("跳过的批进了可定位清单", len(rep.get("skipped_batches") or []) >= 1,
       f"{len(rep.get('skipped_batches') or [])} 条")
    ck("跳过的批没落缓存（续跑要真的重发它们）", rep.get("cache_hits", 0) == 0,
       f"cache_hits={rep.get('cache_hits')}")
    # 越线界：`run.py:623-625` 承诺「只停派发、不杀在飞批」，所以最坏多花
    # 一个「并发 × 单批成本」。这条把那个承诺钉成断言。
    bound = CAP + CONC * PER_BATCH_USD
    ck(f"越线在承诺的界内（<= 上限 + 并发×单批 = ${bound:.4f}）", spent <= bound,
       f"new_spend={spent:.4f}")
    xlsx = sorted(out.glob("*_term.xlsx"))
    ck("**xlsx 照写**（熔断不阻断落盘，续跑才有东西可复用）", len(xlsx) >= 1,
       f"{len(xlsx)} 个")
    ck("**没有 results.zip**（交付包被闸门拦下）",
       not (out / "results.zip").exists())
    ck("报告写出来了", (out / f"report_{rep.get('tag', 'spend_e2e')}.json").exists()
       or bool(list(out.glob("report_*.json"))))
    # 熔断消息里印的「上限」必须**逐字等于**命令行给的值。
    # 原来那里是 `${max_spend:.2f}`，把 $0.005 印成 "$0.01" —— 一句
    # 「新花 $0.0090 超过上限 $0.01」自相矛盾。付费跑里给的是 $11 / $229.77，
    # 看不出问题，但报告不许印与事实不符的数。
    ck("熔断消息印的上限 = 命令行给的值（不是四舍五入过的）",
       f"${CAP:g}" in out_txt and f"${CAP:.2f}" not in out_txt,
       f"找 ${CAP:g}；`${{CAP:.2f}}`={'$.2f 出现过' if f'${CAP:.2f}' in out_txt else '没出现'}")

    # ================= 第二轮：上限给足，不该熔断 =================
    print("\n=== 第二轮：--max-spend 10（上限给足，不该熔断）===")
    rc2, rep2, out2, _txt2 = run_once(["--max-spend", "10"])
    ck("退出码 0", rc2 == 0, f"实际 {rc2}")
    ck("没熔断：stop_reason 为空", not rep2.get("stop_reason"),
       f"stop_reason={rep2.get('stop_reason')!r}")
    ck("四批全部派发（skipped_by_budget == 0）",
       rep2.get("skipped_by_budget", 0) == 0 and rep2.get("batches") == 4,
       f"批={rep2.get('batches')} 跳过={rep2.get('skipped_by_budget')}")
    ck("四批都真的调了 API", rep2.get("calls") == 4, f"calls={rep2.get('calls')}")
    ck("本轮新花 = 4 × 单批（记账口径对得上）",
       abs(spent_of(rep2) - 4 * PER_BATCH_USD) < 1e-6,
       f"{spent_of(rep2):.4f} vs {4 * PER_BATCH_USD:.4f}")
    ck("**results.zip 产出了**（不熔断就该有交付包）",
       (out2 / "results.zip").exists())
finally:
    for d in MADE:
        shutil.rmtree(d, ignore_errors=True)

print()
if FAILED:
    print(f"端到端失败 {len(FAILED)} 项：")
    for x in FAILED:
        print(f"  - {x}")
    sys.exit(1)
print("端到端通过：花费熔断真的停派发、真的拦下交付包、xlsx 照写、越线在界内")
