# 化工园区事故导排网络 · 检修前审计 + 低暴露配流

安全工程师在检修事故导排总管前，录入：

- **一个泄压源**、**一个安全焚烧端**、若干**汇合节点**；
- 若干带**方向**、**最大流量（容量上限）**、**可检修标记**的管段；
- 事故时必须持续排出的流量。

系统在**正常网络**与**每一条可检修管段单独临时失效后的残余网络**上，
分别独立运行最大流（Dinic），给出每个情景的**最大可导排量**。
所有情景均不低于事故要求流量才放行；失败时按管段**录入顺序**
返回首条不达标管段，并依据最大流 / 最小割定理给出可复核的
**源侧割集节点、焚烧端侧节点、割集管段与割集容量**。

审计放行后，安全工程师再为每条管段填写**非负整数单位暴露代价**，
系统生成事故泄压时的**低暴露配流单**：在正常网络与每个单管段失效
残余网络中，分别分配**恰好等于**必须持续排出量的流量（被移除管段
流量固定为零），避免“仅知道可导排，却让更多气体穿过高风险管段”。
所有满足方向、容量与汇合节点守恒的配流中，先取**总代价最低**
（Σ 流量 × 单位暴露代价），再按**管段录入顺序的流量序列字典序**
稳定决胜（排在前面的管段尽量少承担，结果可复现）。

> 结论以**流量**为准而非路径条数：存在多条路径不代表总排量达标，
> 共享瓶颈会限制总流量（见 `tests/test_flow.py::test_shared_bottleneck_not_path_count`）。

## 快速开始（Docker Compose）

```bash
# 构建并启动常驻 Web 服务（默认宿主机端口 8080，可配置）
docker compose up -d --build web
# 浏览器打开 http://localhost:8080

# 自定义宿主机端口
WEB_HOST_PORT=9090 docker compose up -d web
# 或复制 .env.example 为 .env 后修改 WEB_HOST_PORT
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok","service":"flare-audit", ...}
```

## 一次性交付校验服务 verify

`verify` 是一次性服务：依次运行 **pytest 代码测试 → 构建检查
（字节码编译 + 应用导入）→ 真实拉起 uvicorn 的导排 API 冒烟**，
随后自行退出，**退出码即结论**（0 全部通过，非 0 存在失败项）：

```bash
docker compose build verify
docker compose run --rm verify
echo "exit code = $?"
```

冒烟覆盖：健康检查、达标网络放行、失效网络返回首条失效管段且
割集容量 == 最大流、非法节点引用返回 400、低暴露配流单逐情形配流
（情形编号、低代价优先、被移除管段零流量、总代价 = Σ 流量×代价）、
字典序稳定决胜、草稿不放行时不生成配流单、合法微小管段
（无中间节点、单条不可检修管段，容量与需求均为 `5e-10`）放行
且最大流 / 割集 / 总流量 / 管段流量 / 总代价均保留该非零值、
微小容量缺口
（需求 100.0000000005、单干线仅 100，缺口小于算法容差）也不放行、
跨数量级有限容量无关支路（接入死端节点的 1e20 管段）不改变
最大流 / 最小割 / 唯一配流结论、非整数单位暴露代价 400。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                      # 代码测试
python scripts/verify          # 与容器内一致的一次性校验
uvicorn app.main:app --reload  # 开发服务
```

## 业务 API

### `POST /api/audit`

请求体：

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "nodes": ["汇合点A", "汇合点B"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A", "capacity": 100, "maintainable": true}
  ]
}
```

- `nodes` 仅列汇合节点；泄压源与安全焚烧端自动并入节点集合。
- `capacity` 为管段容量上限，必须为正数；方向为 `from → to`，不可逆向。
- 仅 `maintainable: true` 的管段参与“单管段临时失效”模拟。

响应（失败时节选）：

```json
{
  "passed": false,
  "required_flow": 95,
  "normal": {"max_flow": 100, "meets": true, "cut": { ... }},
  "scenarios": [
    {"position": 1, "edge_id": "E1", "from": "...", "to": "...",
     "capacity": 100, "max_flow": 90, "meets": false}
  ],
  "failure": {
    "stage": "single_failure",
    "position": 1,
    "edge_id": "E1",
    "max_flow": 90,
    "required_flow": 95,
    "cut": {
      "capacity": 90,
      "source_side_nodes": ["泄压源V-101", "汇合点B"],
      "sink_side_nodes": ["汇合点A", "焚烧炉F-1"],
      "cut_edges": [ {"position": 3, "id": "E3", "from": "...", "to": "...", "capacity": 90} ]
    }
  }
}
```

割集可独立复核：把节点按 `source_side_nodes / sink_side_nodes` 两分组，
所有从源侧指向焚烧端侧的管段容量之和应恰为 `capacity`，且依据
最大流 / 最小割定理等于该情景最大可导排量。

输入无效（方向自环、容量非正数、节点引用不存在、源汇相同、必填为空等）
返回 `HTTP 400`：

```json
{"error": "第 1 条管段终点“X”未在节点中定义", "field": "edges[0].to"}
```

### `POST /api/plan`

在审计载荷基础上，每条管段增加 `cost`（**非负整数**单位暴露代价，
`5` 与 `5.0` 均可；缺失、负数、小数、字符串、布尔均返回 400）：

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "nodes": ["汇合点A", "汇合点B"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A", "capacity": 100, "maintainable": true, "cost": 2},
    {"id": "E2", "from": "汇合点A", "to": "焚烧炉F-1", "capacity": 100, "maintainable": true, "cost": 2},
    {"id": "E3", "from": "泄压源V-101", "to": "汇合点B", "capacity": 100, "maintainable": true, "cost": 1},
    {"id": "E4", "from": "汇合点B", "to": "焚烧炉F-1", "capacity": 100, "maintainable": true, "cost": 1}
  ]
}
```

服务端**先按既有规则重新审计完整草稿**（草稿问题优先于代价问题
返回 400），审计不放行时不生成配流单（HTTP 200，`passed: false`，
`plans: null`，`audit` 内仍携带首条失效管段与割集证据）。放行后
在正常网络（情形 0）与每条可检修管段单独失效的残余网络
（情形 1..k，按录入顺序）中分别分配**恰好等于**必须持续排出量的
流量，被移除管段流量固定为零：

```json
{
  "passed": true,
  "required_flow": 95,
  "audit": { "passed": true, "normal": {"max_flow": 200, "...": "..."}, "...": "..." },
  "plans": [
    {
      "scenario": 0,
      "stage": "normal",
      "removed": null,
      "flow_value": 95,
      "total_cost": 190,
      "flows": [
        {"position": 1, "edge_id": "E1", "from": "泄压源V-101", "to": "汇合点A",
         "capacity": 100, "cost": 2, "flow": 0, "exposure": 0, "removed": false}
      ]
    },
    {
      "scenario": 1,
      "stage": "single_failure",
      "removed": {"position": 1, "edge_id": "E1", "from": "泄压源V-101", "to": "汇合点A"},
      "flow_value": 95,
      "total_cost": 190,
      "flows": [ {"position": 1, "edge_id": "E1", "flow": 0, "removed": true, "...": "..."} ]
    }
  ]
}
```

配流规则：所有满足方向、容量上限与汇合节点流量守恒的配流中，
先取**总代价最低**（Σ 流量 × 单位暴露代价），再按**管段录入顺序**
的流量序列取**字典序最小**者稳定决胜（排在前面的管段尽量少承担；
结果只取决于录入顺序，与求解器内部细节无关，可复现）。每个情形
在全新的网络上独立求解，互不复用中间流量结果。

### `GET /health`

容器健康检查端点，返回 `{"status":"ok",...}`。

## 前端交互约定

- 页面分区：**当前输入（草稿）**、**输入被拒绝（400）**、**审计结论**、**低暴露配流单**。
- 提交审计后通过真实业务 API 渲染正常网络与逐条失效情形的最大可导排量。
- 通过时显示放行结论；失败时高亮首条失效管段并展示最小割证据。
- 管段表含**单位暴露代价**列（非负整数），仅用于配流单，不影响审计结论。
- 点击“生成低暴露配流单”调用 `POST /api/plan`：服务端先重新审计，
  页面同步刷新审计结论，并在其旁展示各情形配流（情形编号、各管段
  流量、总代价）；草稿未放行时不生成配流单，继续显示首条失效管段
  与割集证据。
- 草稿在上次审计之后被任何修改时，旧结论区顶部出现过期警示；
  草稿或任一单位暴露代价在上次生成之后被修改时，旧配流单立即失效
  并出现失效警示；重新提交后才刷新。

## 项目结构

```
app/
  flow.py            # Dinic 最大流 + 残余网络最小割 + 审计编排与业务校验
  flowplan.py        # 最小费用流（SSP）+ 零费用环字典序决胜 + 配流单编排
  main.py            # FastAPI：/api/audit、/api/plan、/health、静态页面
  static/            # 原生前端（无构建步骤）
tests/               # pytest：引擎/审计/配流逻辑 + API
scripts/verify       # 一次性校验：测试 + 构建 + API 冒烟（退出码报告）
Dockerfile
docker-compose.yml   # web（常驻，健康检查，端口可配）+ verify（一次性）
```
