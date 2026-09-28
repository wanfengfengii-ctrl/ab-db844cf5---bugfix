"""最大流 / 最小割引擎与检修审计逻辑。

规则（对应业务要求）：

* 每条管段被视作一条有向边，录入的最大流量即其容量上限；
* 在**正常网络**与**每一条可检修管段单独移除后的残余网络**上，
  分别独立运行最大流（Dinic），互不复用中间流量结果；
* 事故要求的持续排出流量必须在正常网络和每一个单点失效情景下都可达，
  审计才放行；
* 失效时按管段录入顺序返回第一条不达标管段，并依据最大流 / 最小割定理，
  从残余网络给出可复核的源侧割集、焚烧端侧节点及割集容量。

注意：本模块用“流量”而不是“路径条数”下结论——存在多条路径并不保证
总排量达标，共享瓶颈会限制总流量。
"""
from __future__ import annotations

import math
import sys
from collections import deque
from dataclasses import dataclass
from typing import Optional

# 检修网络节点规模通常不大，放宽递归深度以支持较长的增广链。
sys.setrecursionlimit(100_000)

# 残余容量截断的**相对**容差：仅吸收浮点舍入尾差。
#
# 不能使用绝对阈值（历史上的 EPS=1e-9）：流量整体量级小于该阈值时
# （如容量与排出量均为 5e-10 的合法微小管段），真实残余容量会被
# 当成 0 截断，最大流、最小割与配流全部被错误抹零。实际阈值按求解
# 规模（需求与容量的最大值，见 ``residual_tolerance``）缩放：
# 规模 100 时约 1e-10，规模 5e-10 时约 5e-22——任何真实缺口
# （哪怕只有 5e-10）都仍远大于容差，不会被放行。
RESIDUAL_RTOL = 1e-12

# 达标判定容差（相对）：仅吸收最大流求解的浮点舍入尾差。
# 与残余容量截断 RESIDUAL_RTOL 严格区分：后者是算法内部的残余容量
# 截断，而本容差用于“最大可导排量是否达到必须持续排出量”的业务
# 判定。任何真实容量缺口——哪怕只有 5e-10——都必须判为不达标，
# 不得被容差放行。
MEETS_RTOL = 1e-12


def residual_tolerance(scale: float) -> float:
    """当前流量规模下的残余容量截断阈值（相对容差）。

    规模取网络容量 / 需求的量级（调用方保证为正），因此 5e-10 的
    微小网络阈值约为 5e-22，绝不会把真实非零流量截断为 0。
    """
    return RESIDUAL_RTOL * abs(float(scale))


class NetworkValidationError(ValueError):
    """网络输入无效（节点引用、容量、方向等业务校验失败）。"""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass
class _Edge:
    """Dinic 内部边（带反向残量边索引）。"""

    to: int
    rev: int
    cap: float
    # 配对正/反向边共享流量盒：不通过“原容量 − 残余容量”回读已推送
    # 流量——容量远大于推送量时浮点相减会把真实微小流量抹成 0。
    flowbox: list
    sign: int  # 正向弧 +1，反向残量弧 −1
    # 按**本边自身原始容量**缩放的截断阈值。残余容量的浮点尾差只与
    # 本边容量及经过本边的推送量同量级，与网络中其他跨数量级管段无关：
    # 容量 1 的管段阈值约 1e-12，容量 1e20 的管段阈值约 1e8。
    # 不能用全网统一阈值：一条 1e20 的无关大容量支路会把统一阈值抬到
    # 容量 1 的真实小瓶颈之上，把它的残余容量误判为 0（可行草稿被拒绝，
    # 且最大流证据与按原始容量汇总的最小割自相矛盾）。
    edge_tol: float


class Dinic:
    """容量为非负实数的有向图 Dinic 最大流。

    残余容量截断分两级，避免“跨数量级容量并存”时互相误伤：

    * **正向弧**是否可通行按该弧自身容量缩放（``_Edge.edge_tol``）：
      满载相减产生的 ±容量×ε 尾差恰被本尺度吸收；容量 1 的真实瓶颈
      不会被网络中 1e20 支路的尺度误伤；
    * **反向残量弧**额外用流量规模阈值 ``self.tol`` 收紧：小流量网络
      里大容量管段只可能携带小流量，其反向弧必须保持可通行（改推用），
      而满载相减尾差只可能出现在推送量已达该容量时，彼时流量规模阈值
      已不小于本边阈值，不会漏过尾差。

    ``tol`` 为流量规模阈值（增广量累加 / 停机判定 / 反向弧收紧），按
    求解规模由调用方通过 :func:`residual_tolerance` 给出；无限制求最
    大流时随已得流量自动放大（真实最大流可能远大于事故需求）。
    """

    def __init__(self, n: int, tol: float = RESIDUAL_RTOL):
        self.n = n
        self.tol = tol
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float) -> int:
        """添加有向边，返回正向边在 ``g[u]`` 中的下标（便于回读实际流量）。"""
        flowbox = [0.0]
        edge_tol = residual_tolerance(cap)
        fwd = _Edge(to=v, rev=len(self.g[v]), cap=float(cap),
                    flowbox=flowbox, sign=1, edge_tol=edge_tol)
        bak = _Edge(to=u, rev=len(self.g[u]), cap=0.0,
                    flowbox=flowbox, sign=-1, edge_tol=edge_tol)
        self.g[u].append(fwd)
        self.g[v].append(bak)
        return len(self.g[u]) - 1

    def _is_open(self, e: _Edge) -> bool:
        """残余弧是否可通行：正向按自身容量尺度，反向再受流量规模收紧。"""
        if e.sign == 1:
            return e.cap > e.edge_tol
        return e.cap > e.edge_tol or e.cap > self.tol

    def _bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if self._is_open(e) and level[e.to] < 0:
                    level[e.to] = level[u] + 1
                    q.append(e.to)
        return level

    def _dfs(self, u: int, t: int, pushed: float, level: list[int], it: list[int]) -> float:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            e = self.g[u][it[u]]
            if self._is_open(e) and level[e.to] == level[u] + 1:
                got = self._dfs(e.to, t, min(pushed, e.cap), level, it)
                # 能走到这里的增广路每条弧都已通过**按自身容量尺度**的
                # 残余容量判定（见 _is_open），故任何 got > 0 都是真实
                # 流量，不能再用全网流量规模阈值丢弃：混合量级网络中
                # （容量 1 的直连管段与 1e20 通路并存）先探到的小通路
                # 增量只有 1，丢弃它会跳过仍敞开的弧、破坏 Dinic 当前弧
                # 的推进不变量。大管段帧上 1e20 − 1 为浮点无害空操作，
                # 流量盒与反向弧仍须如实登记这 1 个单位。
                if got > 0.0:
                    e.cap -= got
                    self.g[e.to][e.rev].cap += got
                    e.flowbox[0] += e.sign * got
                    return got
            it[u] += 1
        return 0.0

    def max_flow(self, s: int, t: int, limit: float = float("inf")) -> float:
        """求 s→t 最大流；给定 ``limit`` 时流量达到该上限即提前停止。"""
        flow = 0.0
        inf = float("inf")
        base_tol = self.tol
        while flow < limit - base_tol:
            # 无限制求最大流时真实流量可能远大于初始规模（如事故需求 1
            # 而网络另有 1e20 干线）：随已得流量放大规模阈值，使满载大
            # 容量管段的反向残量尾差始终被正确截断。该阈值只用于停机
            # 判定与反向弧收紧，不用于拒绝真实增广（见 _dfs）。
            self.tol = max(base_tol, residual_tolerance(flow))
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            progressed = False
            while flow < limit - base_tol:
                pushed = self._dfs(s, t, min(inf, limit - flow), level, it)
                if pushed == 0.0:
                    break  # 本分层图当前弧耗尽，重建分层
                flow += pushed
                progressed = True
                self.tol = max(base_tol, residual_tolerance(flow))
            if not progressed:
                # 理论上 BFS 可达汇点就一定有真实增广（所有入路弧均按
                # 自身尺度判定为敞开）；此分支仅作浮点异常的防御性兜底。
                return flow
        return flow

    def reachable_from_source(self, s: int, tol: float | None = None) -> list[bool]:
        """最大流计算后，沿残余容量 > 0 的边做 BFS，得到源侧节点集合。

        ``tol`` 默认为当前**流量规模**阈值（求解结束时的 ``self.tol``），
        对正 / 反向弧统一适用：必须与“已接受的增广量”同一尺度，割集才
        是与回报最大流一致的 s→t 割（割容量 == 最大流）。不能用各边自身
        尺度：大流量已求得后，残余里可能仍有按自身尺度可通行、但相对总
        流量小于 1e-12 的跨数量级微小通路，沿它会直接抵达汇点并把满载
        管段的反向弧也串入源侧，得到容量为 0 的退化割集，与最大流证据
        自相矛盾。
        """
        cut_tol = self.tol if tol is None else tol
        seen = [False] * self.n
        seen[s] = True
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > cut_tol and not seen[e.to]:
                    seen[e.to] = True
                    q.append(e.to)
        return seen


def _clean_name(raw, field: str) -> str:
    if not isinstance(raw, str):
        raise NetworkValidationError(f"{field} 必须是字符串", field)
    name = raw.strip()
    if not name:
        raise NetworkValidationError(f"{field} 不能为空", field)
    return name


def _finite_positive_number(raw, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NetworkValidationError(f"{field} 必须是正数", field)
    value = float(raw)
    if not math.isfinite(value):
        raise NetworkValidationError(f"{field} 必须是有限数值", field)
    if value <= 0:
        raise NetworkValidationError(f"{field} 必须大于 0", field)
    return value


def _validate_draft(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """按既有规则校验草稿，返回清洗后的节点 / 管段结构。

    检修审计与低暴露配流单共用本校验，保证“提交配流单时先按既有
    规则重新审计完整草稿”。

    ``nodes`` 为汇合节点（及其它中间节点）列表；泄压源与安全焚烧端
    自动并入节点集合。``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True}
    """
    source = _clean_name(source, "泄压源")
    sink = _clean_name(sink, "安全焚烧端")
    if source == sink:
        raise NetworkValidationError("泄压源与安全焚烧端不能是同一节点", "sink")

    if isinstance(required_flow, bool) or not isinstance(required_flow, (int, float)):
        raise NetworkValidationError("事故持续排出流量必须是正数", "required_flow")
    required_flow = float(required_flow)
    if not math.isfinite(required_flow) or required_flow <= 0:
        raise NetworkValidationError("事故持续排出流量必须是大于 0 的有限数值", "required_flow")

    if not isinstance(nodes, list):
        raise NetworkValidationError("汇合节点必须是列表", "nodes")

    node_set: set[str] = {source, sink}
    junction_names: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(nodes):
        field = f"nodes[{i}]"
        name = _clean_name(raw, field)
        if name in seen:
            raise NetworkValidationError(f"汇合节点“{name}”重复", field)
        seen.add(name)
        junction_names.append(name)
        node_set.add(name)

    if not isinstance(edges, list):
        raise NetworkValidationError("管段必须是列表", "edges")

    clean_edges: list[dict] = []
    for i, raw in enumerate(edges):
        if not isinstance(raw, dict):
            raise NetworkValidationError(f"第 {i + 1} 条管段格式无效", f"edges[{i}]")
        label = raw.get("id")
        if label is not None and not (isinstance(label, str) and label.strip()):
            label = None
        elif isinstance(label, str):
            label = label.strip()

        u = _clean_name(raw.get("from"), f"第 {i + 1} 条管段起点")
        v = _clean_name(raw.get("to"), f"第 {i + 1} 条管段终点")
        if u not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点“{u}”未在节点中定义", f"edges[{i}].from"
            )
        if v not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段终点“{v}”未在节点中定义", f"edges[{i}].to"
            )
        if u == v:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点和终点不能相同（{u}）", f"edges[{i}].to"
            )
        cap = _finite_positive_number(raw.get("capacity"), f"第 {i + 1} 条管段最大流量")
        maintainable = bool(raw.get("maintainable", False))
        clean_edges.append(
            {
                "index": i,
                "position": i + 1,
                "id": label,
                "from": u,
                "to": v,
                "capacity": cap,
                "maintainable": maintainable,
            }
        )

    all_nodes = sorted(node_set)
    index_of = {name: i for i, name in enumerate(all_nodes)}
    return {
        "source": source,
        "sink": sink,
        "required_flow": required_flow,
        "junction_names": junction_names,
        "edges": clean_edges,
        "all_nodes": all_nodes,
        "index_of": index_of,
    }


def audit_validated_draft(draft: dict) -> dict:
    """在**已校验**的草稿上执行正常网络 + 全部单点失效情景的最大流审计。

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    source = draft["source"]
    sink = draft["sink"]
    required_flow = draft["required_flow"]
    clean_edges = draft["edges"]
    all_nodes = draft["all_nodes"]
    index_of = draft["index_of"]

    def _solve(removed_index: Optional[int]) -> tuple[float, dict]:
        """在一张**全新**的网络上独立求最大流，并返回流量与最小割证据。"""
        active = [e for e in clean_edges if e["index"] != removed_index]
        # 流量规模阈值只用于增广量累加与达标停机判断，按事故必须持续
        # 排出量缩放（与配流端 flowplan._solve_plan 一致）；各边残余
        # 容量是否为零改由该边**自身容量**缩放的阈值独立判定
        # （见 Dinic.add_edge），故跨数量级的无关大容量支路
        # （如接入死端节点的 1e20 管段）不会抬高真实小瓶颈的截断尺度。
        tol = residual_tolerance(required_flow)
        dinic = Dinic(len(all_nodes), tol)
        for e in active:
            dinic.add_edge(index_of[e["from"]], index_of[e["to"]], e["capacity"])
        value = dinic.max_flow(index_of[source], index_of[sink])
        side = dinic.reachable_from_source(index_of[source])

        source_side = sorted(all_nodes[k] for k, ok in enumerate(side) if ok)
        sink_side = sorted(all_nodes[k] for k, ok in enumerate(side) if not ok)
        cut_edges = []
        cut_capacity = 0.0
        for e in active:  # 按录入顺序列出，便于复核
            if side[index_of[e["from"]]] and not side[index_of[e["to"]]]:
                cut_edges.append(
                    {
                        "index": e["index"],
                        "position": e["position"],
                        "id": e["id"],
                        "from": e["from"],
                        "to": e["to"],
                        "capacity": _num(e["capacity"]),
                    }
                )
                cut_capacity += e["capacity"]
        return value, {
            "capacity": _num(cut_capacity),
            "source_side_nodes": source_side,
            "sink_side_nodes": sink_side,
            "cut_edges": cut_edges,
        }

    def _meets(value: float) -> bool:
        # 严格达标：容差仅按浮点尾差量级（相对 1e-12）吸收舍入误差，
        # 最大可导排量严格小于必须持续排出量时一律判为不达标。
        return value + MEETS_RTOL * max(1.0, abs(required_flow)) >= required_flow

    # 1) 正常网络
    normal_value, normal_cut = _solve(None)

    # 2) 每条可检修管段单独临时失效（残余网络独立求解）
    scenarios = []
    failure = None
    for e in clean_edges:
        if not e["maintainable"]:
            continue
        value, cut = _solve(e["index"])
        scenario = {
            "edge_index": e["index"],
            "position": e["position"],
            "edge_id": e["id"],
            "from": e["from"],
            "to": e["to"],
            "capacity": _num(e["capacity"]),
            "max_flow": _num(value),
            "meets": _meets(value),
        }
        scenarios.append(scenario)
        # 按管段录入顺序取首条不达标者
        if failure is None and not _meets(value):
            failure = {
                "stage": "single_failure",
                "edge_index": e["index"],
                "position": e["position"],
                "edge_id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "capacity": _num(e["capacity"]),
                "required_flow": _num(required_flow),
                "max_flow": _num(value),
                "cut": cut,
            }

    # 没有任何可检修管段时，至少正常网络本身必须达标
    if failure is None and not scenarios and not _meets(normal_value):
        failure = {
            "stage": "normal",
            "edge_index": None,
            "position": None,
            "edge_id": None,
            "from": None,
            "to": None,
            "capacity": None,
            "required_flow": _num(required_flow),
            "max_flow": _num(normal_value),
            "cut": normal_cut,
        }

    return {
        "passed": failure is None and _meets(normal_value),
        "required_flow": _num(required_flow),
        "normal": {
            "max_flow": _num(normal_value),
            "meets": _meets(normal_value),
            "cut": normal_cut,
        },
        "scenarios": scenarios,
        "failure": failure,
    }


def audit_network(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """校验输入并执行正常网络 + 全部单点失效情景的最大流审计。

    ``nodes`` 为汇合节点（及其它中间节点）列表；泄压源与安全焚烧端
    自动并入节点集合。``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True}

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    draft = _validate_draft(
        source=source, sink=sink, nodes=nodes, edges=edges, required_flow=required_flow
    )
    return audit_validated_draft(draft)


def _num(x: float) -> float:
    """消除浮点尾差，便于展示与复核（如 0.30000000000000004）。

    按**有效数字**（而非固定小数位）规整：固定 ``round(x, 6)`` 会把
    5e-10 这类合法微小非零值抹成 0。对小于 1e-6 的值按相对量级取
    有效数字，使其保留真实非零值的同时仍能消去浮点尾差。
    """
    x = float(x)
    if x == 0.0:
        return 0.0
    magnitude = math.floor(math.log10(abs(x)))
    # 常规量级仍按 6 位小数规整（保持既有展示语义）；微小量级改为
    # 保留 12 位有效数字（如 5e-10、100.0000000005 一类数值）。
    if magnitude >= -6:
        ndigits = 6
    else:
        ndigits = 12 - 1 - magnitude
    r = round(x, ndigits)
    return 0.0 if r == 0 else r
