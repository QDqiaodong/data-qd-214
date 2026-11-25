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
    .venv/bin/python examples/run_identity_sample.py

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

### 出口车次身份解歧与受控回填

分析员可显式指出需要重新识别标签的出口，档案先给出所有全局最优解释所支持的
身份范围，再只把确定（forced）的关联回填为一次普通来源修订：

```python
plan = archive.plan_identities("s1", ["x", "y"], 0, 60, 20, 1, "plan-req")
archive.read_identity_plan(plan["plan_id"])
result = archive.apply_identities(plan["plan_id"], "apply-req")
```

计划不改变修订号、包或取用；回填只更正 forced 出口的车次标签，轴数差异原样
保留（仍为 `axle_mismatch`）。执行前会按当前观测重新核对身份范围、原始证据和
既有配对，任一条件不满足就整次拒绝。候选入口来自“排除指定出口后仍真实
occupied 的入口”，而不是所有入口。多条最优解释会让出口保持 `ambiguous`、
不可回填。详见 [docs/reconciliation-archive.md](docs/reconciliation-archive.md)。

### 离线复位核销与更正防护

分析员拿到已批准的历史复位记录后，可在一次提交里同时更正观测并登记/撤回复位：

```python
result = archive.revise_with_resets(
    "reset-req", revision, upserts, deletes,
    add_resets=[{
        "id": "r1",
        "section_id": "s1",
        "time": "2026-04-11T00:00:00Z",
        "authorization_ref": "AUTH-1",
        "opening": [entry_event],   # 审批覆盖的完整未闭合入口，至少一条
    }],
    withdraw_reset_ids=[],
)
archive.read_reset("r1")           # 规范正文 + active；撤回不删历史
archive.reviewed_day("s1", "2026-04-11")  # 复位感知的复核视图，不产生包或凭据
```

复位在同刻观测之前处理；只有当“当时未闭合入口全集”与 opening 五字段逐条
完全一致时才核销这些入口，不产生 movement、不伪造观测，也不抹掉此前异常。
`reviewed_day` 与原始日包并存：旧 `seal`/`current`/`take` 仍按原始观测语义
工作。任何会推翻已登记复位所依据事实的观测更正或身份回填都会被整批拒绝，
除非在同一批更正里明确撤回受影响的复位；纯复位变动不增加来源修订号，也不
撤回原始包和取用凭据。

完整语义、崩溃恢复故障注入点和测试说明见 [docs/reconciliation-archive.md](docs/reconciliation-archive.md)。

完整语义、崩溃恢复故障注入点和测试说明见 [docs/reconciliation-archive.md](docs/reconciliation-archive.md)。

## 当前边界

每个检查点当前只属于一个独立区段，不处理线路拓扑、折返、复线方向或设备复位审批。异常均保留原事件身份，不自动修正观测。

交接档案只表示离线数据版本交接，不连接或控制信号设备，也不代表区段安全认证。档案不要求并发写入、跨机器协调或磁盘介质损坏恢复。
