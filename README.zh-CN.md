# term-extractor-multilingual

从句级对齐的同传语料里抽取双语术语对，每个输入文件出一张 xlsx。
中 ↔ 西 / 法 / 俄。

[English](README.md)

## 安装

```bash
pip install -r requirements.txt
```

俄语需要 `pymorphy3-dicts-ru`；缺了它，流水线会拒绝开跑。

## 用法

```bash
# 断言套件 —— 不需要语料，也不需要 API key
python -m getterms.tests

# 端到端脚本 —— 用 fixtures/ 里的合成语料
python e2e/e2e_delivery_columns.py

# 真跑一次
python -m getterms.run --lang es --model gemini-3.7-flash --effort high \
    --concurrency 40 --out out/es
```

语料放在 `corpus/<lang>/`（可用 `CORPUS_ROOT` 改）。API key 走环境变量 `API_KEY`
（或各供应商自己的变量），也可以放进 `keys/`。`--dry-run` 不写盘、不调接口。

## 输出

每个输入文件一张 `*_term.xlsx`，sheet 名 `terms`：

```
sent_id | src_text | tgt_text | term_src | term_tgt | types | note | term_src_dict | term_tgt_dict
```

`term_src` / `term_tgt` 是该行两个句子里的**原样切片**，必须逐字可查；
`term_src_dict` / `term_tgt_dict` 是词典形（ISO 10241-1）。
`types` 取自 10 个术语类别，不抽 `num`；`note` 恒为空。

## 协议

源码可见，其他用途须事先书面同意——见 [LICENSE](LICENSE)。
