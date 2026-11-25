# data-qd-214 · 铁路轴计数对账

把区段两端轴计数事件按列车和时间配对，区分正常通过、轴数不一致、无入口出口、重复入口和仍占用区段。

本仓只做离线运行数据核对，不控制信号设备、不作安全认证。字段与失败边界见 docs/contract.md。

## 安装、测试、演示

Python 3.11+；运行仅标准库，构建固定 setuptools 80.9.0 和 wheel 0.45.1。

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python --offline -r requirements-build.lock
    uv pip install --python .venv/bin/python --offline --no-index --no-build-isolation -e .
    .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python examples/run_sample.py

样例中 freight-8 入口 64 轴、出口 63 轴，形成 axle_mismatch；service-2 只有入口，因此保留在 occupied。

## 两种输入模式

公开 API 始终是 `axle_reconciliation.process(data)`，不提供通用 CLI。

- **旧格式（兼容保留）**：`sections` + 顶层 `events`，事件 ID 全局唯一。结果只含 `movements`、`anomalies`、`occupied` 三个键，行为与历史版本完全一致。
- **批次格式**：`sections` + `batches`，不得再给顶层 `events`。`batches` 为 1..1000 个采集批次，每项含全局唯一的非空 `id`、带时区的 `received_at` 和非空 `events` 数组；原始事件副本总数不超过 100000。

批次模式在事件进入对账前做归一化，解决断线重连、补传和重复投递：

- **等价重放**：同一事件 ID 的业务字段（`train_id`、`checkpoint`、`axles` 及换算到 UTC 后相同的 `time`）完全一致时，只生成一个规范事件；规范副本来自 `(received_at, batch.id)` 最早的批次。输入数组顺序不影响结果，等价副本不会被重复计数。
- **冲突隔离**：同一 ID 只要存在两种不同业务观测，该 ID 的全部副本都进入隔离区，不做多数票、最新批次或顺序裁决，任何版本都不进入 `movements`、`anomalies`、`occupied`。例如出口被隔离时入口保留在 `occupied`，入口被隔离时出口成为 `exit_without_entry`。
- **审计输出**：批次模式额外返回顶层 `ingestion`，含升序的 `accepted_event_ids`、`quarantined_event_ids`，以及按事件 ID 升序的 `duplicates`（全部来源批次与规范批次）和 `conflicts`（保留每个批次 ID 与原始事件字段的全部副本）。同一 ID 不会同时出现在 `duplicates` 与 `conflicts`。

完整字段与失败边界见 docs/contract.md；批次示例见 fixtures/batch_input.json，对应测试见 tests/test_batches.py。

## 当前边界

每个检查点当前只属于一个独立区段，不处理线路拓扑、折返、复线方向或设备复位审批。异常均保留原事件身份，不自动修正观测。
