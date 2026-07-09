# Benchmark Runner

Evaluate **Academic Compass** retrieval quality on standardized benchmarks.

## Quick Start

```bash
# 有哪些 benchmark 可用？
python scripts/benchmark/run.py list

# 跑 AutoResearchBench Wide Research（Level 1），返回 30 篇
python scripts/benchmark/run.py run autoresearchbench --type wide --top-k 30

# 先烟测 5 条看看速度
python scripts/benchmark/run.py run scholargym --type selection --top-k 10 --max-queries 5

# 对比同一 Level 下的两个版本
python scripts/benchmark/run.py diff autoresearchbench --type wide --level 1
```

## 支持的 Benchmark

| key | 名称 | 类型 | 题目数 | ground truth | 指标 |
|-----|------|------|--------|--------------|------|
| `autoresearchbench` | AutoResearchBench | `wide` | 400 | arXiv ID 列表 (2-34篇) | IoU / Recall / Precision |
| `autoresearchbench` | AutoResearchBench | `deep` | 600 | arXiv ID 或 No-Answer | hit_at_k / MRR |
| `scholargym` | ScholarGym | `selection` | 2,536 | arXiv ID 列表 (1-62篇) | Recall / Precision / F1 |

## 用法

### `python scripts/benchmark/run.py run <benchmark> --type <type> [options]`

| 参数 | 默认 | 说明 |
|------|------|------|
| `--top-k` | `10` | 每条 query 搜索引擎返回几篇 |
| `--level` | `1` | 搜索级别：1=论文级, 2=段落级, 3=Agent 全管线 |
| `--version` | 自动递增 | 版本标签，如 `v1.0` |
| `--max-queries` | 全量 | 只跑前 N 条，用于快速烟测 |

### `python scripts/benchmark/run.py diff <benchmark> --type <type> [--level N]`

对比最近两个版本的指标变化。

## 版本号

版本号按 Level 独立管理。`l1/v1.0` 和 `l2/v1.0` 互不干扰。

```bash
# 第一次跑 → data/benchmark/autoresearchbench/wide/l1/v1.0/
python scripts/benchmark/run.py run autoresearchbench --type wide --top-k 30

# 修了 bug 再跑 → l1/v1.1/（自动 +0.1）
python scripts/benchmark/run.py run autoresearchbench --type wide --top-k 30

# 手动指定大版本
python scripts/benchmark/run.py run autoresearchbench --type wide --top-k 30 --version v2.0
```

## 输出结构

```
data/benchmark/
├── autoresearchbench/
│   ├── wide/
│   │   ├── l1/
│   │   │   ├── v1.0/
│   │   │   │   ├── results.json        ← 聚合指标
│   │   │   │   └── per_query.json      ← 逐题详情
│   │   │   └── v1.1/
│   │   └── l2/        （Level 2 实现后自动出现）
│   └── deep/
│       └── l1/
│           └── v1.0/
└── scholargym/
    └── selection/
        └── l1/
            └── v1.0/
```

### `results.json`

```json
{
  "timestamp": "2026-06-01T20:30:00+08:00",
  "git_commit": "5aa5bc8",
  "params": { "top_k": 30, "level": 1, "query_count": 400 },
  "metrics": {
    "avg_iou": 0.124,
    "avg_recall": 0.156,
    "avg_precision": 0.203
  }
}
```

### `per_query.json`

```json
[
  {
    "query_id": "wide_0010",
    "question": "From 2022 to 2025, what papers on ...",
    "gt_arxiv_ids": ["2212.10368", "2508.00913"],
    "predicted_arxiv_ids": ["2508.00913", "2205.11111"],
    "hit_ids": ["2508.00913"],
    "missed_ids": ["2212.10368"],
    "iou": 0.25,
    "recall": 0.5,
    "precision": 0.333,
    "search_ms": 84.6
  }
]
```

## 注意：BM25 冷启动

第一次 `run` 时（每次 `uv run` 都是新进程），需要 ~5s 反序列化 BM25 checkpoint。
这是 CLI 模式的硬开销，不会随二次调用消失。未来 API server 模式下只用加载一次。

## 扩展：添加新 Benchmark

1. 在 `registry.py` 注册 `BenchmarkSpec`
2. 在 `runners/` 新建 runner，继承 `BaseRunner`
3. 实现 `_load_queries()` 和 `_aggregate()`
