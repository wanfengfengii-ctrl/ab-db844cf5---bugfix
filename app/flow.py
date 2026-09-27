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
    sign: int


class Dinic:
    """容量为非负实数的有向图 Dinic 最大流。

    ``tol`` 为残余容量截断阈值，按求解规模由调用方通过
    :func:`residual_tolerance` 计算，避免用固定绝对阈值抹掉
    量级很小的真实流量。
    """

    def __init__(self, n: int, tol: float = RESIDUAL_RTOL):
        self.n = n
        self.tol = tol
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float) -> int:
        """添加有向边，返回正向边在 ``g[u]`` 中的下标（便于回读实际流量）。"""
        flowbox = [0.0]
        fwd = _Edge(to=v, rev=len(self.g[v]), cap=float(cap),
                    flowbox=flowbox, sign=1)
        bak = _Edge(to=u, rev=len(self.g[u]), cap=0.0,
                    flowbox=flowbox, sign=-1)
        self.g[u].append(fwd)
        self.g[v].append(bak)
        return len(self.g[u]) - 1

    def _bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > self.tol and level[e.to] < 0:
                    level[e.to] = level[u] + 1
                    q.append(e.to)
        return level

    def _dfs(self, u: int, t: int, pushed: float, level: list[int], it: list[int]) -> float:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            e = self.g[u][it[u]]
            if e.cap > self.tol and level[e.to] == level[u] + 1:
                got = self._dfs(e.to, t, min(pushed, e.cap), level, it)
                if got > self.tol:
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
        while flow < limit - self.tol:
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            while flow < limit - self.tol:
                pushed = self._dfs(s, t, min(inf, limit - flow), level, it)
                if pushed <= self.tol:
                    break
                flow += pushed
        return flow

    def reachable_from_source(self, s: int) -> list[bool]:
        """最大流计算后，沿残余容量 > 0 的边做 BFS，得到源侧节点集合。"""
        seen = [False] * self.n
        seen[s] = True
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > self.tol and not seen[e.to]:
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
        src_idx, sink_idx = index_of[source], index_of[sink]

        def _build(tol: float) -> Dinic:
            dinic = Dinic(len(all_nodes), tol)
            for e in active:
                dinic.add_edge(index_of[e["from"]], index_of[e["to"]], e["capacity"])
            return dinic

        # 截断阈值必须按**实际推送流量**的量级缩放，而非网络中最大管段
        # 容量：残余容量的浮点舍入噪声只来自真实发生的推送（量级不超过
        # 最大流本身），一条不承载任何流量的无关管段不会带来任何噪声。
        # 历史上按 max(需求, 全部容量) 取规模：一条容量 1e20 的无关支路
        # （如 S→死端汇合点 X）会把 tol 抬到 1e8，把容量仅 1 的唯一
        # S→T 干线在 BFS 中当成零残余剪掉——最大流被误算为 0，而割集
        # 容量按原始容量求和仍为 1，出现互相矛盾的流量/割证据。
        #
        # 真实最大流事先未知，故先以必须持续排出量为规模求解；若实际
        # 最大流超出该量级（如需求 95、真实可导 200），再在全新网络上
        # 按实测流量重新求解一次，使截断阈值与真实舍入噪声匹配。粗阈值
        # 只会剪掉更多弧，重解结果不可能大于首轮结果，因此至多重解一次。
        scale = required_flow
        dinic = _build(residual_tolerance(scale))
        value = dinic.max_flow(src_idx, sink_idx)
        if value > scale + dinic.tol:
            scale = value
            dinic = _build(residual_tolerance(scale))
            value = dinic.max_flow(src_idx, sink_idx)
        side = dinic.reachable_from_source(src_idx)

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
