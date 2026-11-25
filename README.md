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

不安装为可编辑包时，也可直接从仓库根目录运行：

    PYTHONPATH=src python3 -m unittest discover -s tests -v

样例中 freight-8 入口 64 轴、出口 63 轴，形成 axle_mismatch；service-2 只有入口，因此保留在 occupied。

## 运行日交接档案

`ReconciliationArchive` 在本地持久化按 UTC 日签发的离线交接包、跨日未闭合入口、下游取用凭据和幂等请求回执：

```python
from axle_reconciliation import ReconciliationArchive

archive = ReconciliationArchive(
    "./handoff-archive",
    [{"id": "s1", "entry": "a", "exit": "b"}],
    "2026-04-10",
    "2026-04-11",
)
archive.revise("req-1", 0, events, [])
package = archive.seal("s1", "2026-04-10", "seal-1")
receipt = archive.take(package["package_id"], "take-1")
```

交接日以 UTC 午夜为界；后一日包必须绑定前一自然日的当前有效包。来源观测发生实际更正时，旧位置对应区段从最早受影响 UTC 日起的有效包和取用凭据会被撤回，但历史包正文仍可通过 `read_package` 追溯。成功请求 ID 可安全重放：重放得到首次成功的历史结果，而 `read_package` / `read_take` 表示这些历史包和凭据现在是否仍有效。

完整语义、崩溃恢复故障注入点和测试说明见 [docs/reconciliation-archive.md](docs/reconciliation-archive.md)。

## 当前边界

每个检查点当前只属于一个独立区段，不处理线路拓扑、折返、复线方向或设备复位审批。异常均保留原事件身份，不自动修正观测。

交接档案只表示离线数据版本交接，不连接或控制信号设备，也不代表区段安全认证。档案不要求并发写入、跨机器协调或磁盘介质损坏恢复。
