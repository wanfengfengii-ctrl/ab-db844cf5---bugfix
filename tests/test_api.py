"""业务 API 测试：健康检查、审计放行/失败证据、非法输入 400、静态页面。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASS_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ],
}

FAIL_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ],
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "事故导排" in r.text


def test_audit_pass():
    r = client.post("/api/audit", json=PASS_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["normal"]["max_flow"] == 200
    assert len(body["scenarios"]) == 4
    assert all(s["meets"] for s in body["scenarios"])
    assert body["failure"] is None


def test_audit_fail_returns_first_edge_and_cut():
    r = client.post("/api/audit", json=FAIL_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    f = body["failure"]
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1


def test_audit_invalid_node_reference_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "不存在", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "未在节点中定义" in r.json()["error"]


def test_audit_invalid_capacity_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "T", "capacity": 0, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "大于 0" in r.json()["error"]


def test_audit_invalid_direction_self_loop_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "S", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "起点和终点不能相同" in r.json()["error"]


def test_audit_non_json_body_400():
    r = client.post("/api/audit", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------- 低暴露配流单 POST /api/plan ----------------

PLAN_PASS_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True, "cost": 2},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True, "cost": 2},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True, "cost": 1},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True, "cost": 1},
    ],
}


def test_plan_pass_returns_all_scenarios():
    r = client.post("/api/plan", json=PLAN_PASS_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    # 服务端先重新审计完整草稿，结论内嵌返回
    assert body["audit"]["passed"] is True
    plans = body["plans"]
    # 正常网络（情形 0）+ 四条可检修管段逐一失效（情形 1..4）
    assert [p["scenario"] for p in plans] == [0, 1, 2, 3, 4]
    # 正常网络走低暴露 B 干线（单位代价 1）
    assert [f["flow"] for f in plans[0]["flows"]] == [0, 0, 95, 95]
    assert plans[0]["total_cost"] == 190
    # 情形 1：E1 被移除，流量固定为零
    p1 = plans[1]
    assert p1["removed"]["position"] == 1
    assert p1["flows"][0]["flow"] == 0 and p1["flows"][0]["removed"] is True
    assert [f["flow"] for f in p1["flows"]] == [0, 0, 95, 95]
    # 每种情形恰好排出必须持续排出量
    for p in plans:
        assert p["flow_value"] == 95
        assert p["total_cost"] == sum(f["flow"] * f["cost"] for f in p["flows"])


def test_plan_blocked_when_audit_fails_is_still_200():
    """草稿不放行：HTTP 200 + passed=false，不生成配流单，保留首条失效管段与割集。"""
    r = client.post("/api/plan", json=FAIL_PAYLOAD | {
        "edges": [dict(e, cost=1) for e in FAIL_PAYLOAD["edges"]]
    })
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert body["plans"] is None
    f = body["audit"]["failure"]
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["cut"]["capacity"] == 90


def test_plan_tiny_capacity_gap_not_waived():
    """微小但真实的容量缺口（需求 100.0000000005，单干线仅 100）不得放行。"""
    payload = {
        "source": "S",
        "sink": "T",
        "required_flow": 100.0000000005,
        "nodes": ["A", "B"],
        "edges": [
            dict(e, cost=1) for e in (
                {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
                {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
                {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
                {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
            )
        ],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert body["plans"] is None
    assert body["audit"]["passed"] is False
    f = body["audit"]["failure"]
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 100.0
    assert f["cut"]["capacity"] == 100
    # 同一微小缺口在 /api/audit 上同样不放行
    r2 = client.post("/api/audit", json={k: v for k, v in payload.items() if k != "edges"} | {
        "edges": [{k: v for k, v in e.items() if k != "cost"} for e in payload["edges"]]
    })
    assert r2.json()["passed"] is False


def test_plan_tiny_legitimate_flow_passes_with_nonzero_evidence():
    """合法微小管段（容量 / 需求均为 5e-10）：放行且保留非零流量证据。"""
    payload = {
        "source": "S",
        "sink": "T",
        "required_flow": 5e-10,
        "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 5e-10,
             "maintainable": False, "cost": 1},
        ],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["plans"] is not None
    plan = body["plans"][0]
    assert plan["flow_value"] == 5e-10
    assert plan["flows"][0]["flow"] == 5e-10
    assert plan["total_cost"] == 5e-10
    assert body["audit"]["normal"]["max_flow"] == 5e-10
    assert body["audit"]["normal"]["cut"]["capacity"] == 5e-10
    # 同一草稿在 /api/audit 上也放行并保留非零最大流
    r2 = client.post("/api/audit", json={
        "source": "S", "sink": "T", "required_flow": 5e-10, "nodes": [],
        "edges": [{"id": "E1", "from": "S", "to": "T",
                   "capacity": 5e-10, "maintainable": False}],
    })
    audit = r2.json()
    assert audit["passed"] is True
    assert audit["normal"]["max_flow"] == 5e-10


def test_plan_huge_irrelevant_branch_keeps_feasible_draft():
    """POST /api/plan：1e20 无关支路不影响唯一可行配流，仍 200 + passed=true。"""
    payload = {
        "source": "S",
        "sink": "T",
        "required_flow": 1,
        "nodes": ["X"],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 1,
             "maintainable": False, "cost": 1},
            {"id": "E2", "from": "S", "to": "X", "capacity": 1e20,
             "maintainable": False, "cost": 0},
        ],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["audit"]["normal"]["max_flow"] == 1
    assert body["audit"]["normal"]["cut"]["capacity"] == 1
    plans = body["plans"]
    assert len(plans) == 1
    plan = plans[0]
    assert plan["flow_value"] == 1
    assert plan["total_cost"] == 1
    assert [f["flow"] for f in plan["flows"]] == [1, 0]
    # 同一草稿在 /api/audit 上也放行
    r2 = client.post("/api/audit", json={
        "source": "S", "sink": "T", "required_flow": 1, "nodes": ["X"],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 1, "maintainable": False},
            {"id": "E2", "from": "S", "to": "X", "capacity": 1e20, "maintainable": False},
        ],
    })
    assert r2.status_code == 200
    audit = r2.json()
    assert audit["passed"] is True
    assert audit["normal"]["max_flow"] == 1
    assert audit["normal"]["cut"]["capacity"] == 1


def test_plan_missing_cost_400():
    r = client.post("/api/plan", json=PASS_PAYLOAD)  # 审计载荷没有 cost
    assert r.status_code == 400
    err = r.json()
    assert "单位暴露代价" in err["error"]
    assert err["field"].startswith("edges[") and err["field"].endswith("].cost")


def test_plan_negative_cost_400():
    payload = {
        "source": "S", "sink": "T", "required_flow": 1, "nodes": [],
        "edges": [{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": -1}],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 400
    assert "不能为负数" in r.json()["error"]


def test_plan_non_integer_cost_400():
    payload = {
        "source": "S", "sink": "T", "required_flow": 1, "nodes": [],
        "edges": [{"from": "S", "to": "T", "capacity": 10, "maintainable": False, "cost": 2.5}],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 400
    assert "非负整数" in r.json()["error"]


def test_plan_draft_validation_takes_priority_400():
    payload = {
        "source": "S", "sink": "T", "required_flow": 1, "nodes": [],
        "edges": [{"from": "S", "to": "幽灵", "capacity": 10, "maintainable": False}],
    }
    r = client.post("/api/plan", json=payload)
    assert r.status_code == 400
    assert "未在节点中定义" in r.json()["error"]


def test_plan_non_json_body_400():
    r = client.post("/api/plan", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
