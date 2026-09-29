# -*- coding: utf-8 -*-
r"""端到端：余额不足时，run.py 真的会停下来吗？逃生口真的能放行吗？

纯函数判据证明不了**熔断接线**是否真的生效，所以这里真跑 `run.py`，
只把 `LLMClient` 换成一个「所有请求都返回 403 余额不足」的子类
（复用全部真实逻辑：重试判定、stats、缓存、熔断），看：

**第一轮（默认）**：退出码 3 / balance_errors 记上 / **第二批被跳过**（熔断的全部意义）
/ stop_reason / 跳过清单可定位 / 失败的批没落缓存。

**第二轮（--allow-no-balance）**：熔断**不**触发，两批都派发、都失败、跳过为 0。
这一轮专门验逃生口 —— argparse 的 `-` 到 `_` 转换容易接错，
`--allow-no-balance` 必须真的接上 `a.allow_no_balance`。

**零 API 调用**（假 client 不发 HTTP）。语料只取 1 个文件的前 20 句，
批大小 10 -> 2 批，正好够看出「第一批撞墙、第二批被拦」。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

import httpx
import openai

# 本脚本从 `e2e/` 子目录运行，先把仓库根加进 sys.path。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# ⚠ 本脚本**从不发真实请求**（假 client 或假 HTTP 层），但 `LLMClient.__init__`
#   会读一次 key 才肯构造。给个哑值即可 —— 环境变量优先于 key 文件，真 key 碰不到。
#   这样一来这个脚本**任何人拿到都能跑**，不需要先配一把真 key。
os.environ.setdefault("API_KEY", "sk-dummy-e2e-never-sends-a-request")
# 未提供生产语料 —— 用 fixtures/ 里的**合成夹具**（内容全部虚构）。
from getterms import config as _cfg
_cfg.CORPUS_DIRS["es"] = (pathlib.Path(__file__).resolve().parent.parent
                          / "fixtures" / "corpus_es")


import getterms.run as R
from getterms.llm import LLMClient

# ⚠ 绕过了 `run.py` 底部的 `if __name__ == "__main__": utf8_stdout()` ——
#   不显式设一次，本脚本的成败会取决于控制台编码（UTF-8 过 / GBK 挂）。
from getterms.config import utf8_stdout

utf8_stdout()

FAILED = []
MADE = []       # 临时目录，收尾统一清（含临时 cache_root）


def ck(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


class BalanceDeadClient(LLMClient):
    """所有请求都撞 403 余额不足。复用父类全部真实逻辑。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.http_attempts = 0
        # ⚠ **必须把缓存指到临时目录**：否则一旦缓存命中就不发假请求，
        #   测试结果取决于缓存状态而不是代码。
        self._tmp_cache = tempfile.mkdtemp(prefix="bal_e2e_cache_")
        self.cache_root = pathlib.Path(self._tmp_cache)
        MADE.append(self._tmp_cache)

        async def _create(**kw2):
            self.http_attempts += 1
            raise openai.PermissionDeniedError(
                "API Error: 403 Your account balance is insufficient.",
                response=httpx.Response(
                    403, request=httpx.Request("POST", "https://x/v1")),
                body=None)

        self._client.chat.completions.create = _create


def run_once(extra):
    """跑一轮，返回 (退出码, 报告 dict)。"""
    tmp = tempfile.mkdtemp(prefix="bal_e2e_")
    MADE.append(tmp)
    argv = [
        "--lang", "es", "--model", "gemini-3.7-flash", "--effort", "high",
        "--limit-files", "1", "--limit-sentences", "20",
        "--out", tmp, "--tag", "bal_e2e",
        "--concurrency", "1", "--force", "--no-zip", "--skip-preflight",
    ] + extra
    try:
        rc = R.main(argv)
        js = sorted(pathlib.Path(tmp).glob("report_*.json"))
        rep = json.loads(js[-1].read_text(encoding="utf-8")) if js else {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, rep


R.LLMClient = BalanceDeadClient      # ← 只换这一处，其余全是真的

print("=== 第一轮：默认（余额不足应触发熔断）===")
rc, rep = run_once([])
ck("退出码是 3（交付包被闸门拦下）", rc == 3, f"实际 {rc}")
ck("报告记了 balance_errors", rep.get("balance_errors", 0) >= 1,
   f"balance_errors={rep.get('balance_errors')}")
ck("stop_reason 是「余额不足」", rep.get("stop_reason") == "余额不足",
   f"stop_reason={rep.get('stop_reason')!r}")
ck("**后续批被跳过**（熔断的全部意义）", rep.get("skipped_by_budget", 0) >= 1,
   f"skipped_by_budget={rep.get('skipped_by_budget')}")
ck("跳过的批进了可定位清单", len(rep.get("skipped_batches") or []) >= 1,
   f"{len(rep.get('skipped_batches') or [])} 条")
ck("失败的批没落缓存 -> 充值后重跑会精确重试", rep.get("cache_hits", 0) == 0,
   f"cache_hits={rep.get('cache_hits')}")
if rep.get("skipped_batches"):
    print(f"  跳过清单首条: {rep['skipped_batches'][0]}")

print("\n=== 第二轮：--allow-no-balance（逃生口应放行）===")
rc2, rep2 = run_once(["--allow-no-balance"])
ck("逃生口接对了：熔断不触发（跳过为 0）",
   rep2.get("skipped_by_budget", 0) == 0,
   f"skipped_by_budget={rep2.get('skipped_by_budget')} —— 非 0 说明 "
   f"--allow-no-balance 没接上 a.allow_no_balance")
ck("两批都派发、都失败", rep2.get("batches") == 2
   and rep2.get("call_failures") == 2,
   f"批={rep2.get('batches')} 失败={rep2.get('call_failures')}")
ck("stop_reason 为空（没停过）", not rep2.get("stop_reason"),
   f"stop_reason={rep2.get('stop_reason')!r}")
ck("余额不足仍然计数（逃生口只关熔断，不关计数）",
   rep2.get("balance_errors", 0) >= 1,
   f"balance_errors={rep2.get('balance_errors')}")

# 收尾：临时 out 与临时 cache_root 一起清。
for _d in MADE:
    shutil.rmtree(_d, ignore_errors=True)

print()
if FAILED:
    print(f"端到端失败 {len(FAILED)} 项：")
    for x in FAILED:
        print(f"  - {x}")
    sys.exit(1)
print("端到端通过：余额不足时停下来、后续批被跳过、清单可定位、退出码 3；"
      "逃生口能放行")
