"""低暴露配流单（最小费用流 + 字典序决胜）逻辑测试。

重点验证业务约束：
* 先按既有规则重新审计完整草稿，不放行则不生成配流单；
* 每个情形（正常网络 + 每条可检修管段单独失效）分配**恰好**
  等于事故必须持续排出量的流量，被移除管段流量固定为零；
* 满足方向、容量、汇合节点守恒的所有配流中，先取总代价最低
  （Σ 流量×单位暴露代价），再按管段录入顺序的流量序列字典序决胜；
* 单位暴露代价必须为每条管段填写的非负整数。
"""
import pytest

from app.flowplan import plan_low_exposure
from app.flow import NetworkValidationError


def _assert_feasible(plan, edges, required, removed_index=None):
    """独立复核一个配流情形：恰好排出量、容量、守恒、移除管段零流量。"""
    flows = [f["flow"] for f in plan["flows"]]
    assert len(flows) == len(edges)
    # 恰好等于必须持续排出量（以源净流出量复核）
    assert plan["flow_value"] == required
    bal = {}
    for k, (f, e) in enumerate(zip(flows, edges)):
        assert f >= 0
        assert f <= e["capacity"]  # 容量上限
        bal[e["from"]] = bal.get(e["from"], 0.0) + f
        bal[e["to"]] = bal.get(e["to"], 0.0) - f
        if k == removed_index:
            assert f == 0 and plan["flows"][k]["removed"] is True
        else:
            assert plan["flows"][k]["removed"] is False
    assert bal["S"] == required
    assert bal["T"] == -required
    for n, v in bal.items():  # 汇合节点守恒
        if n not in ("S", "T"):
            assert v == 0, (n, v)
    # 总代价 = Σ 流量 × 单位暴露代价
    assert plan["total_cost"] == sum(f * e["cost"] for f, e in zip(flows, edges))
    return flows


def _parallel_pass(costs):
    """两条 100 并联干线 S→A→T、S→B→T，可检修，要求 95。"""
    return {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": ["A", "B"],
        "edges": [
            {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "cost": costs[0]},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "cost": costs[1]},
            {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True, "cost": costs[2]},
            {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True, "cost": costs[3]},
        ],
    }


def test_audit_rerun_and_scenario_layout():
    r = plan_low_exposure(**_parallel_pass([1, 1, 1, 1]))
    assert r["passed"] is True
    # 内嵌的是既有规则重新审计的完整结论
    assert r["audit"]["passed"] is True
    assert r["audit"]["normal"]["max_flow"] == 200
    plans = r["plans"]
    # 情形 0 为正常网络，情形 1..4 为四条可检修管段按录入顺序逐一失效
    assert [p["scenario"] for p in plans] == [0, 1, 2, 3, 4]
    assert plans[0]["stage"] == "normal" and plans[0]["removed"] is None
    for k in (1, 2, 3, 4):
        assert plans[k]["stage"] == "single_failure"
        assert plans[k]["removed"]["position"] == k


def test_exact_flow_conservation_and_removed_zero():
    kw = _parallel_pass([1, 1, 1, 1])
    r = plan_low_exposure(**kw)
    for k, plan in enumerate(r["plans"]):
        removed = None if k == 0 else k - 1
        _assert_feasible(plan, kw["edges"], 95, removed)
    # 情形 1（E1 失效）：全部走 B 干线
    assert [f["flow"] for f in r["plans"][1]["flows"]] == [0, 0, 95, 95]


def test_low_cost_preferred_avoids_high_exposure():
    """低代价管段优先；只有容量不足时才动用高风险（高代价）管段。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 50, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": False, "cost": 1},
            {"id": "E2", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "cost": 7},
        ],
    }
    r = plan_low_exposure(**kw)
    flows = _assert_feasible(r["plans"][0], kw["edges"], 50)
    assert flows == [30, 20]
    assert r["plans"][0]["total_cost"] == 30 * 1 + 20 * 7


def test_lexicographic_tiebreak_by_input_order():
    """总代价并列时，按录入顺序取流量序列字典序最小者（前面的管段尽量少承担）。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 50, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "cost": 5},
            {"id": "E2", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "cost": 5},
        ],
    }
    r = plan_low_exposure(**kw)
    flows = _assert_feasible(r["plans"][0], kw["edges"], 50)
    # [0,50] 与 [50,0] 总代价相同，字典序 [0,50] 最小
    assert flows == [0, 50]


def test_lexicographic_tiebreak_order_follows_input_sequence():
    """决胜只取决于录入顺序：交换录入顺序，结果随之交换（稳定决胜）。"""
    def build(edge_order):
        base = [
            {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False, "cost": 1},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": False, "cost": 1},
            {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": False, "cost": 1},
            {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": False, "cost": 1},
        ]
        return {
            "source": "S", "sink": "T", "required_flow": 50, "nodes": ["A", "B"],
            "edges": [base[i] for i in edge_order],
        }

    r = plan_low_exposure(**build([0, 1, 2, 3]))
    # 录入顺序 E1,E2,E3,E4：字典序最小为 E1=E2=0，流量走 B 干线
    assert [f["flow"] for f in r["plans"][0]["flows"]] == [0, 0, 50, 50]
    # 录入顺序 E3,E4,E1,E2：排在前面的 E3,E4 被压到 0，流量改由后录入的 E1,E2 承担
    r2 = plan_low_exposure(**build([2, 3, 0, 1]))
    assert [f["flow"] for f in r2["plans"][0]["flows"]] == [0, 0, 50, 50]
    assert [f["edge_id"] for f in r2["plans"][0]["flows"]] == ["E3", "E4", "E1", "E2"]


def test_lexicographic_prefers_later_when_all_zero_cost():
    """全零代价（纯字典序）复杂网络：排在最前的管段流量被压到最小。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 50, "nodes": ["A", "B"],
        "edges": [
            {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False, "cost": 0},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": False, "cost": 0},
            {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": False, "cost": 0},
            {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": False, "cost": 0},
            {"id": "E5", "from": "A", "to": "B", "capacity": 100, "maintainable": False, "cost": 0},
        ],
    }
    r = plan_low_exposure(**kw)
    flows = _assert_feasible(r["plans"][0], kw["edges"], 50)
    assert flows == [0, 0, 50, 50, 0]
    assert r["plans"][0]["total_cost"] == 0


def test_cheapest_bottleneck_forced_flow_not_reduced():
    """更便宜的紧瓶颈管段即使排在前面也不能为决胜而降流（任何替代都更贵）。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 50, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": False, "cost": 1},
            {"id": "E2", "from": "S", "to": "T", "capacity": 100, "maintainable": False, "cost": 7},
        ],
    }
    r = plan_low_exposure(**kw)
    flows = [f["flow"] for f in r["plans"][0]["flows"]]
    # E1 虽排第一，但它是最便宜的紧容量边：必须满载 30，不能压成 0
    assert flows == [30, 20]


def test_tiny_capacity_gap_blocks_plan():
    """单管段失效后 100 < 需求 100.0000000005：微小真实缺口也不放行配流单。"""
    kw = _parallel_pass([1, 1, 1, 1])
    kw["required_flow"] = 100.0000000005
    r = plan_low_exposure(**kw)
    assert r["passed"] is False
    assert r["plans"] is None
    f = r["audit"]["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 100.0
    assert f["cut"]["capacity"] == 100
    # 需求恰好等于单干线容量时仍正常放行，配流语义不变
    kw_exact = _parallel_pass([1, 1, 1, 1])
    kw_exact["required_flow"] = 100
    r2 = plan_low_exposure(**kw_exact)
    assert r2["passed"] is True
    assert [p["scenario"] for p in r2["plans"]] == [0, 1, 2, 3, 4]
    assert all(p["flow_value"] == 100 for p in r2["plans"])


def test_blocked_when_audit_does_not_pass():
    """草稿不放行：不得生成配流单，继续给出首条失效管段与割集证据。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": ["A", "B"],
        "edges": [
            {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "cost": 1},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "cost": 1},
            {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True, "cost": 1},
            {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True, "cost": 1},
        ],
    }
    r = plan_low_exposure(**kw)
    assert r["passed"] is False
    assert r["plans"] is None
    f = r["audit"]["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    # 既有割集证据继续保留
    assert f["cut"]["capacity"] == 90
    assert "S" in f["cut"]["source_side_nodes"]
    assert "T" in f["cut"]["sink_side_nodes"]
    assert f["cut"]["cut_edges"]


def test_blocked_when_normal_network_insufficient():
    kw = {
        "source": "S", "sink": "T", "required_flow": 95, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 50, "maintainable": False, "cost": 1},
        ],
    }
    r = plan_low_exposure(**kw)
    assert r["passed"] is False and r["plans"] is None
    assert r["audit"]["failure"]["stage"] == "normal"


def test_plan_per_scenario_independent():
    """每个情形在独立残余网络上求解，互不串流量。"""
    kw = _parallel_pass([2, 2, 1, 1])  # B 干线更便宜
    r = plan_low_exposure(**kw)
    # 正常网络全部走低暴露 B 干线
    assert [f["flow"] for f in r["plans"][0]["flows"]] == [0, 0, 95, 95]
    assert r["plans"][0]["total_cost"] == 190
    # 情形 3（E3 失效）：B 干线入口断，只能走高代价 A 干线
    p3 = r["plans"][3]
    assert [f["flow"] for f in p3["flows"]] == [95, 95, 0, 0]
    assert p3["total_cost"] == 380
    _assert_feasible(p3, kw["edges"], 95, removed_index=2)


@pytest.mark.parametrize("bad", [-1, -100])
def test_negative_cost_rejected(bad):
    with pytest.raises(NetworkValidationError) as exc:
        plan_low_exposure(
            source="S", sink="T", nodes=[],
            edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": bad}],
            required_flow=1,
        )
    assert "不能为负数" in str(exc.value)
    assert exc.value.field == "edges[0].cost"


@pytest.mark.parametrize("bad", [1.5, 0.01, "3", [3], 3.0001])
def test_non_integer_cost_rejected(bad):
    with pytest.raises(NetworkValidationError) as exc:
        plan_low_exposure(
            source="S", sink="T", nodes=[],
            edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": bad}],
            required_flow=1,
        )
    assert "非负整数" in str(exc.value)
    assert exc.value.field == "edges[0].cost"


@pytest.mark.parametrize("bad", [None])
def test_missing_cost_rejected(bad):
    with pytest.raises(NetworkValidationError) as exc:
        plan_low_exposure(
            source="S", sink="T", nodes=[],
            edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False}],
            required_flow=1,
        )
    assert "必须填写" in str(exc.value)
    assert exc.value.field == "edges[0].cost"


def test_boolean_cost_rejected():
    with pytest.raises(NetworkValidationError):
        plan_low_exposure(
            source="S", sink="T", nodes=[],
            edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": True}],
            required_flow=1,
        )


def test_tiny_legitimate_flow_generates_unique_plan():
    """合法微小管段（容量 / 需求均为 5e-10）通过审计并生成唯一配流。

    回归：绝对截断阈值曾把 5e-10 的真实流量抹成 0，审计误判不达标、
    plans=null。现总流量、该管段流量与总代价均应为 5e-10。
    """
    kw = {
        "source": "S", "sink": "T", "required_flow": 5e-10, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 5e-10,
             "maintainable": False, "cost": 1},
        ],
    }
    r = plan_low_exposure(**kw)
    assert r["passed"] is True
    assert r["plans"] is not None
    assert len(r["plans"]) == 1
    plan = r["plans"][0]
    assert plan["scenario"] == 0 and plan["removed"] is None
    assert plan["flow_value"] == 5e-10
    assert plan["total_cost"] == 5e-10
    edge = plan["flows"][0]
    assert edge["flow"] == 5e-10
    assert edge["exposure"] == 5e-10
    assert edge["capacity"] == 5e-10
    assert edge["removed"] is False
    # 内嵌审计同样保留非零最大流 / 割集容量
    audit = r["audit"]
    assert audit["normal"]["max_flow"] == 5e-10
    assert audit["normal"]["cut"]["capacity"] == 5e-10


def test_tiny_flow_not_zeroed_even_beside_huge_capacity():
    """小排出量不会因网络中存在超大容量管段而被截断阈值抹零。"""
    kw = {
        "source": "S", "sink": "T", "required_flow": 5e-10, "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 1e20,
             "maintainable": False, "cost": 1},
        ],
    }
    r = plan_low_exposure(**kw)
    assert r["passed"] is True
    assert r["plans"][0]["flow_value"] == 5e-10
    assert r["plans"][0]["flows"][0]["flow"] == 5e-10
    assert r["plans"][0]["total_cost"] == 5e-10


def test_integer_valued_float_cost_accepted():
    r = plan_low_exposure(
        source="S", sink="T", nodes=[],
        edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": 5.0}],
        required_flow=3,
    )
    assert r["passed"] is True
    assert r["plans"][0]["flows"][0]["cost"] == 5
    assert r["plans"][0]["total_cost"] == 15


def test_zero_cost_allowed():
    r = plan_low_exposure(
        source="S", sink="T", nodes=[],
        edges=[{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": 0}],
        required_flow=4,
    )
    assert r["plans"][0]["total_cost"] == 0
    assert r["plans"][0]["flows"][0]["exposure"] == 0


def test_cost_field_position_follows_input_order():
    """代价错误定位到具体录入顺序的管段。"""
    edges = [
        {"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": 1},
        {"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": "x"},
    ]
    with pytest.raises(NetworkValidationError) as exc:
        plan_low_exposure(source="S", sink="T", nodes=[], edges=edges, required_flow=1)
    assert exc.value.field == "edges[1].cost"


def test_draft_validated_before_costs():
    """草稿本身无效时优先按既有规则拒绝（即便没有填代价）。"""
    with pytest.raises(NetworkValidationError) as exc:
        plan_low_exposure(
            source="S", sink="T", nodes=[],
            edges=[{"from": "S", "to": "幽灵节点", "capacity": 10, "maintainable": False}],
            required_flow=1,
        )
    assert "未在节点中定义" in str(exc.value)
