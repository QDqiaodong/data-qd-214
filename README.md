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

## 当前边界

每个检查点当前只属于一个独立区段，不处理线路拓扑、折返、复线方向或设备复位审批。异常均保留原事件身份，不自动修正观测。
