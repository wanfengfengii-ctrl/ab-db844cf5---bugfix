"""最大流引擎与审计逻辑测试。

重点验证业务约束：
* 管段容量是上限，方向不可逆向；
* 用流量而非路径条数下结论（共享瓶颈场景）；
* 每个单点失效情景在独立残余网络上求解；
* 失败时按录入顺序返回首条失效管段，且割集容量 == 该情景最大流。
"""
import pytest

from app.flow import NetworkValidationError, audit_network


def _base_edges():
    """两条 100 干线并联：S→A→T 与 S→B→T。"""
    return [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ]


def test_normal_and_each_single_failure_independent():
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=_base_edges(), required_flow=95)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 200
    assert len(r["scenarios"]) == 4
    # 任一干线管段失效后仍剩 100（独立求解，不串流量）
    assert all(s["max_flow"] == 100 and s["meets"] for s in r["scenarios"])
    assert r["failure"] is None


def test_shared_bottleneck_not_path_count():
    """两条路径共享 50 瓶颈：路径数=2 但最大流只有 50。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False},
        {"id": "E2", "from": "S", "to": "B", "capacity": 100, "maintainable": False},
        {"id": "E3", "from": "A", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E4", "from": "B", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E5", "from": "C", "to": "T", "capacity": 50, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B", "C"],
                      edges=edges, required_flow=60)
    assert r["normal"]["max_flow"] == 50
    assert r["passed"] is False
    # 无可检修管段时，正常网络不达标也要报失败
    assert r["failure"]["stage"] == "normal"
    assert r["failure"]["cut"]["capacity"] == 50


def test_first_failing_edge_by_input_order_and_cut():
    """首条失效管段按录入顺序；割集容量等于该情景最大流。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert r["passed"] is False
    f = r["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90 == f["max_flow"]          # 最大流 = 最小割
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1
    # 割边方向必须源侧 → 焚烧端侧
    for e in cut["cut_edges"]:
        assert e["from"] in cut["source_side_nodes"]
        assert e["to"] in cut["sink_side_nodes"]
    # 情景表仍按录入顺序完整列出
    assert [s["position"] for s in r["scenarios"]] == [1, 2, 3, 4]
    assert r["scenarios"][0]["meets"] is False
    assert r["scenarios"][1]["meets"] is False
    assert r["scenarios"][2]["meets"] is True


def test_tiny_capacity_gap_below_epsilon_is_not_waived():
    """微小但真实的容量缺口（5e-10）不得被算法容差放行。

    两条 100 干线并联，需求 100.0000000005：正常网络可导 200，
    但任一单管段失效后只剩一条 100 干线，严格小于需求，必须判
    不达标，并按录入顺序保留首条失效管段与割集证据。
    """
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=_base_edges(), required_flow=100.0000000005)
    assert r["passed"] is False
    assert r["normal"]["meets"] is True  # 正常网络 200 ≥ 需求
    # 四个单点失效情景残余最大流都是 100，严格小于需求
    assert all(s["max_flow"] == 100 and s["meets"] is False for s in r["scenarios"])
    f = r["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"  # 录入顺序首条
    assert f["max_flow"] == 100.0
    # 注：required_flow 经 _num 展示规整为 100.0（既有展示语义），
    # 放行判定仍以提交的精确值 100.0000000005 为准
    cut = f["cut"]
    assert cut["capacity"] == 100 == f["max_flow"]
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert cut["cut_edges"]


def test_exact_requirement_still_passes():
    """边界对照：需求恰好等于单干线容量 100 时继续放行（无真实缺口）。"""
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=_base_edges(), required_flow=100)
    assert r["passed"] is True
    assert all(s["meets"] for s in r["scenarios"])


def test_tiny_legitimate_nonzero_flow_is_preserved():
    """合法微小管段（容量与需求均为 5e-10）必须放行且保留非零证据。

    回归：绝对截断阈值 1e-9 曾把该量级的真实残余容量当成 0，导致
    最大流 / 割集容量 / 配流全部被错误抹零。
    """
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 5e-10, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=5e-10)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 5e-10
    assert r["normal"]["meets"] is True
    assert r["normal"]["cut"]["capacity"] == 5e-10
    assert r["normal"]["cut"]["cut_edges"][0]["capacity"] == 5e-10
    assert r["failure"] is None


def test_tiny_network_still_detects_real_shortfall():
    """微小量级下真实缺口依旧不放行（相对容差不放宽判定）。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 5e-10, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=[], edges=edges,
                      required_flow=5e-10 * 1.5)
    assert r["passed"] is False
    assert r["normal"]["max_flow"] == 5e-10
    assert r["normal"]["cut"]["capacity"] == 5e-10


def test_meets_tolerance_only_absorbs_float_rounding():
    """达标容差只吸收浮点舍入尾差（约 1e-16 量级），不放宽真实缺口。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 0.1, "maintainable": False},
        {"id": "E2", "from": "S", "to": "T", "capacity": 0.7, "maintainable": False},
    ]
    # 0.1 + 0.7 的浮点结果相对 0.8 仅有约 1e-17 尾差，仍应判达标
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=0.8)
    assert r["passed"] is True
    # 同样小量级的真实缺口（需求多出 5e-10）必须判不达标
    r2 = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=0.8000000005)
    assert r2["passed"] is False
    assert r2["failure"]["stage"] == "normal"


def test_direction_is_enforced():
    """方向不可逆向：T→S 的边不能用来从 S 导流到 T。"""
    edges = [
        {"id": "E1", "from": "T", "to": "S", "capacity": 100, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=1)
    assert r["normal"]["max_flow"] == 0
    assert r["passed"] is False


def test_capacity_is_upper_bound_parallel_edges_sum():
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": True},
        {"id": "E2", "from": "S", "to": "T", "capacity": 70, "maintainable": True},
    ]
    # 正常网络：并联容量相加 = 100
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=100)
    assert r["normal"]["max_flow"] == 100
    assert r["normal"]["meets"] is True
    # 但任一可检修管段失效后只剩另一条，整体审计不得通过
    assert r["passed"] is False
    # 要求 71：移除第 1 条(30) 后只剩 70，按录入顺序首条失效即 #1
    r2 = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=71)
    assert r2["passed"] is False
    assert r2["failure"]["position"] == 1
    assert r2["failure"]["max_flow"] == 70


def test_non_maintainable_edges_not_simulated():
    edges = _base_edges() + [
        {"id": "E5", "from": "A", "to": "B", "capacity": 10, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert len(r["scenarios"]) == 4  # E5 不参与失效模拟
    assert all(s["edge_id"] != "E5" for s in r["scenarios"])


def test_junction_nodes_and_source_sink_auto_registered():
    """泄压源/焚烧端不必出现在 nodes 列表中。"""
    r = audit_network(source="S", sink="T", nodes=[],
                      edges=[{"from": "S", "to": "T", "capacity": 5, "maintainable": False}],
                      required_flow=5)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 5


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(source="S", sink="S", nodes=[], edges=[], required_flow=1), "不能是同一节点"),
        (dict(source="S", sink="T", nodes=["A", "A"], edges=[], required_flow=1), "重复"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "X", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "X", "to": "T", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "S", "capacity": 1}], required_flow=1), "起点和终点不能相同"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": 0}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": -3}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": "大"}], required_flow=1), "正数"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=0), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=-1), "大于 0"),
        (dict(source="", sink="T", nodes=[], edges=[], required_flow=1), "不能为空"),
    ],
)
def test_invalid_inputs_rejected(kwargs, needle):
    with pytest.raises(NetworkValidationError) as exc:
        audit_network(**kwargs)
    assert needle in str(exc.value)


def test_huge_irrelevant_branch_does_not_block_feasible_flow():
    """跨数量级的无关大容量支路不得改变唯一小通路的审计结论。

    回归：截断阈值曾按全网最大单管段容量缩放，接入死端汇合节点的
    1e20 支路把 tol 抬到 1e8，唯一 S→T 管段（容量 1）的残余容量被
    误判为 0：最大流被算成 0 而最小割仍是 1，可行草稿被拒绝且流量
    与割证据自相矛盾。
    """
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 1, "maintainable": False},
        {"id": "E2", "from": "S", "to": "X", "capacity": 1e20, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["X"], edges=edges, required_flow=1)
    assert r["passed"] is True
    assert r["failure"] is None
    assert r["normal"]["max_flow"] == 1
    assert r["normal"]["meets"] is True
    # 最小割仍只含唯一通向 T 的管段，1e20 死端支路不在割集中
    cut = r["normal"]["cut"]
    assert cut["capacity"] == 1
    assert [e["id"] for e in cut["cut_edges"]] == ["E1"]
    assert "X" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]


def test_huge_edges_along_path_with_tiny_internal_bottleneck():
    """路径两端 1e20、中间瓶颈 1：最大流与最小割只反映真实瓶颈 1。"""
    edges = [
        {"id": "E1", "from": "S", "to": "X", "capacity": 1e20, "maintainable": False},
        {"id": "E2", "from": "X", "to": "Y", "capacity": 1, "maintainable": False},
        {"id": "E3", "from": "Y", "to": "T", "capacity": 1e20, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["X", "Y"],
                      edges=edges, required_flow=1)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 1
    assert r["normal"]["cut"]["capacity"] == 1
    assert [e["id"] for e in r["normal"]["cut"]["cut_edges"]] == ["E2"]


def test_parallel_huge_and_tiny_routes_both_counted():
    """容量 1 的直连管段与 1e20 通路并联：最大流须为两者之和，割容量一致。

    回归：曾用全网统一阈值，小通路增广量 1 被大流量尺度阈值丢弃，
    当前弧指针又越过仍敞开的小边，导致最大流被算成 0。
    """
    edges = [
        {"id": "BIG1", "from": "S", "to": "A", "capacity": 1e20, "maintainable": False},
        {"id": "BIG2", "from": "A", "to": "T", "capacity": 1e20, "maintainable": False},
        {"id": "SMALL", "from": "S", "to": "T", "capacity": 1, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A"], edges=edges, required_flow=1)
    assert r["passed"] is True
    # 浮点下 1e20 + 1 == 1e20，但小通路绝不能让大通路也被丢掉
    assert r["normal"]["max_flow"] == 1e20
    assert r["normal"]["cut"]["capacity"] == 1e20
    # 仅按大通路即可满足 1e20 的大需求（小边先被探到时也不能误杀求解）
    r_big = audit_network(source="S", sink="T", nodes=["A"], edges=edges,
                          required_flow=1e20)
    assert r_big["passed"] is True
    assert r_big["normal"]["max_flow"] == 1e20
    assert r_big["normal"]["cut"]["capacity"] == 1e20


def test_tiny_route_explored_first_still_finds_huge_route():
    """录入顺序把容量 1 的直连边排在 1e20 通路之前时，求解结论不变。"""
    edges = [
        {"id": "SMALL", "from": "S", "to": "T", "capacity": 1, "maintainable": False},
        {"id": "BIG1", "from": "S", "to": "A", "capacity": 1e20, "maintainable": False},
        {"id": "BIG2", "from": "A", "to": "T", "capacity": 1e20, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A"], edges=edges,
                      required_flow=1e20)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 1e20
    assert r["normal"]["cut"]["capacity"] == 1e20


def test_huge_irrelevant_branch_without_real_path_still_zero():
    """对照：只有 1e20 死端支路、不存在 S→T 通路时依旧判 0 不放行。"""
    edges = [
        {"id": "E2", "from": "S", "to": "X", "capacity": 1e20, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["X"], edges=edges, required_flow=1)
    assert r["passed"] is False
    assert r["normal"]["max_flow"] == 0
    assert r["normal"]["cut"]["capacity"] == 0
    assert r["failure"]["stage"] == "normal"


def test_real_tiny_gap_still_detected_beside_huge_branch():
    """1e20 无关支路不得掩盖真实微小容量缺口（5e-10）。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 1, "maintainable": False},
        {"id": "E2", "from": "S", "to": "X", "capacity": 1e20, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["X"],
                      edges=edges, required_flow=1.0000000005)
    assert r["passed"] is False
    assert r["normal"]["max_flow"] == 1
    assert r["normal"]["cut"]["capacity"] == 1
    assert r["failure"]["stage"] == "normal"


def test_cut_capacity_equals_max_flow_normal():
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 40, "maintainable": True},
        {"id": "E2", "from": "S", "to": "B", "capacity": 60, "maintainable": True},
        {"id": "E3", "from": "A", "to": "T", "capacity": 50, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 50, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=1)
    # S→A 限 40，B→T 限 50，合计 90
    assert r["normal"]["max_flow"] == 90
    assert r["normal"]["cut"]["capacity"] == 90
