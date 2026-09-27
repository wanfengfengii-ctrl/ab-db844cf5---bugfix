/* 事故导排网络检修审计 —— 前端逻辑
 *
 * 职责边界：前端只负责录入草稿与展示**服务端业务 API** 返回的结论，
 * 不在本地计算任何最大流 / 最小费用流 / 判定。草稿一旦在上次审计后
 * 被改动，旧结论立即标记为过期；草稿或任一单位暴露代价在上次生成
 * 配流单后被改动，旧配流单立即失效，都不会被当作当前草稿的结果。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    nodes: [],          // 汇合节点名称（不含源/汇）
    edges: [],          // {id, from, to, capacity, cost, maintainable}
    lastAuditSignature: null,  // 上次成功审计时草稿的签名（不含代价）
    lastPlanSignature: null,   // 上次成功生成配流单时草稿的签名（含代价）
  };

  /* ---------------- 示例数据 ---------------- */

  // 达标示例：两条 100 容量干线在源/汇之间并联，要求 95，
  // 任一可检修管段失效后仍至少剩 100 容量；B 干线单位暴露代价更低。
  const EXAMPLE_PASS = {
    source: "泄压源V-101",
    sink: "焚烧炉F-1",
    required_flow: 95,
    nodes: ["汇合点A", "汇合点B"],
    edges: [
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, cost: 2, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, cost: 2, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 100, cost: 1, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 100, cost: 1, maintainable: true },
      { id: "E5", from: "汇合点A", to: "汇合点B", capacity: 40, cost: 0, maintainable: false },
    ],
  };

  // 失效示例：正常网络最大流 100，但首条可检修管段 E1 失效后
  // 上干线路径中断，仅剩 90，低于要求 95。
  const EXAMPLE_FAIL = {
    source: "泄压源V-101",
    sink: "焚烧炉F-1",
    required_flow: 95,
    nodes: ["汇合点A", "汇合点B"],
    edges: [
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, cost: 1, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, cost: 1, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 90, cost: 1, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 90, cost: 1, maintainable: true },
    ],
  };

  /* ---------------- 草稿渲染 ---------------- */

  function renderNodes() {
    const box = $("nodes-list");
    box.innerHTML = "";
    if (state.nodes.length === 0) {
      box.innerHTML = '<span class="tip">尚无汇合节点，点击右上方按钮添加。</span>';
      return;
    }
    state.nodes.forEach((name, i) => {
      const chip = document.createElement("span");
      chip.className = "node-chip";
      const input = document.createElement("input");
      input.type = "text";
      input.value = name;
      input.placeholder = "节点名称";
      input.addEventListener("input", () => { state.nodes[i] = input.value; markDirty(); });
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "×";
      del.title = "删除该汇合节点";
      del.addEventListener("click", () => { state.nodes.splice(i, 1); renderAll(); markDirty(); });
      chip.append(input, del);
      box.appendChild(chip);
    });
  }

  function renderEdges() {
    const body = $("edges-body");
    body.innerHTML = "";
    state.edges.forEach((edge, i) => {
      const tr = document.createElement("tr");

      const tdNo = document.createElement("td");
      tdNo.textContent = String(i + 1);

      const tdId = document.createElement("td");
      const idInput = document.createElement("input");
      idInput.type = "text";
      idInput.value = edge.id || "";
      idInput.placeholder = "选填";
      idInput.addEventListener("input", () => { edge.id = idInput.value; markDirty(); });
      tdId.appendChild(idInput);

      const tdFrom = document.createElement("td");
      const fromInput = document.createElement("input");
      fromInput.type = "text";
      fromInput.value = edge.from;
      fromInput.placeholder = "起点节点";
      fromInput.addEventListener("input", () => { edge.from = fromInput.value; markDirty(); });
      tdFrom.appendChild(fromInput);

      const tdArrow = document.createElement("td");
      tdArrow.className = "arrow";
      tdArrow.textContent = "→";

      const tdTo = document.createElement("td");
      const toInput = document.createElement("input");
      toInput.type = "text";
      toInput.value = edge.to;
      toInput.placeholder = "终点节点";
      toInput.addEventListener("input", () => { edge.to = toInput.value; markDirty(); });
      tdTo.appendChild(toInput);

      const tdCap = document.createElement("td");
      const capInput = document.createElement("input");
      capInput.type = "number";
      capInput.min = "0";
      capInput.step = "any";
      capInput.value = edge.capacity;
      capInput.addEventListener("input", () => { edge.capacity = capInput.value; markDirty(); });
      tdCap.appendChild(capInput);

      const tdCost = document.createElement("td");
      const costInput = document.createElement("input");
      costInput.type = "number";
      costInput.min = "0";
      costInput.step = "1";
      costInput.value = edge.cost === "" || edge.cost === null || edge.cost === undefined ? "" : edge.cost;
      costInput.placeholder = "非负整数";
      costInput.title = "每单位流量穿过该管段的暴露代价（非负整数），仅用于低暴露配流单";
      costInput.addEventListener("input", () => { edge.cost = costInput.value; markDirty(); });
      tdCost.appendChild(costInput);

      const tdMaint = document.createElement("td");
      tdMaint.className = "center";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!edge.maintainable;
      cb.title = "勾选后参与“单管段临时失效”模拟";
      cb.addEventListener("change", () => { edge.maintainable = cb.checked; markDirty(); });
      tdMaint.appendChild(cb);

      const tdDel = document.createElement("td");
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "row-del";
      delBtn.textContent = "×";
      delBtn.title = "删除该管段";
      delBtn.addEventListener("click", () => { state.edges.splice(i, 1); renderEdges(); markDirty(); });
      tdDel.appendChild(delBtn);

      tr.append(tdNo, tdId, tdFrom, tdArrow, tdTo, tdCap, tdCost, tdMaint, tdDel);
      body.appendChild(tr);
    });
  }

  function renderAll() {
    renderNodes();
    renderEdges();
  }

  /* ---------------- 草稿状态 / 旧结论与旧配流单过期 ---------------- */

  // 审计载荷：既有规则，不含单位暴露代价
  function currentPayload() {
    const required = $("in-required").value;
    return {
      source: $("in-source").value.trim(),
      sink: $("in-sink").value.trim(),
      required_flow: required === "" ? null : Number(required),
      nodes: state.nodes.map((n) => n.trim()).filter((n) => n.length),
      edges: state.edges.map((e) => ({
        id: (e.id || "").trim() || null,
        from: (e.from || "").trim(),
        to: (e.to || "").trim(),
        capacity: e.capacity === "" || e.capacity === null ? null : Number(e.capacity),
        maintainable: !!e.maintainable,
      })),
    };
  }

  // 配流单载荷：在审计载荷基础上为每条管段附上单位暴露代价
  function planPayload() {
    const payload = currentPayload();
    payload.edges.forEach((edge, i) => {
      const raw = state.edges[i].cost;
      edge.cost = raw === "" || raw === null || raw === undefined ? null : Number(raw);
    });
    return payload;
  }

  // 用稳定签名判断“草稿是否在上次审计/生成后被修改”
  function signature() {
    return JSON.stringify(currentPayload());
  }

  function planSignature() {
    return JSON.stringify(planPayload());
  }

  function markDirty() {
    let hint = "";
    if (state.lastAuditSignature !== null) {
      const stale = signature() !== state.lastAuditSignature;
      $("stale-banner").classList.toggle("hidden", !stale);
      if (stale) hint = "草稿已修改，结论区显示的是旧结论，请重新提交审计。";
    }
    if (state.lastPlanSignature !== null) {
      const stale = planSignature() !== state.lastPlanSignature;
      $("plan-stale-banner").classList.toggle("hidden", !stale);
      if (stale) hint = "草稿或单位暴露代价已修改，配流单已失效，请重新生成。";
    }
    $("draft-hint").textContent = hint;
  }

  function clearResult() {
    $("result-card").classList.add("hidden");
    $("reject-card").classList.add("hidden");
    $("plan-card").classList.add("hidden");
    $("stale-banner").classList.add("hidden");
    $("plan-stale-banner").classList.add("hidden");
    $("draft-hint").textContent = "";
  }

  /* ---------------- 结论渲染 ---------------- */

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    const x = Number(n);
    if (x === 0) return "0";
    // 常规量级保留至多 6 位小数；微小非零值（< 1e-6）按 12 位有效
    // 数字补足小数位，避免把 5e-10 这类真实流量显示成 0。
    const magnitude = Math.floor(Math.log10(Math.abs(x)));
    // toLocaleString 的 maximumFractionDigits 上限为 20。
    const maxFrac = Math.min(20, magnitude >= -6 ? 6 : 12 - 1 - magnitude);
    return x.toLocaleString("zh-CN", { maximumFractionDigits: maxFrac });
  }

  function edgeLabel(e) {
    const id = e.edge_id ? `（${e.edge_id}）` : "";
    return `第 ${e.position} 条管段${id}：${e.from} → ${e.to}`;
  }

  function renderChips(el, names, cls) {
    el.innerHTML = "";
    names.forEach((n) => {
      const c = document.createElement("span");
      c.className = "chip " + (cls || "");
      c.textContent = n;
      el.appendChild(c);
    });
    if (names.length === 0) {
      const c = document.createElement("span");
      c.className = "chip empty";
      c.textContent = "（无）";
      el.appendChild(c);
    }
  }

  function cutEdgeRows(cut, withSides) {
    return cut.cut_edges.map((e) => {
      const cells = withSides
        ? [e.position, e.id || "—", e.from, "→", e.to, fmt(e.capacity)]
        : [e.position, e.id || "—", `${e.from} → ${e.to}`, fmt(e.capacity)];
      return "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
    }).join("");
  }

  function renderResult(data) {
    const card = $("result-card");
    card.classList.remove("hidden");
    $("reject-card").classList.add("hidden");
    $("stale-banner").classList.add("hidden");
    $("draft-hint").textContent = "";

    const passed = !!data.passed;
    $("pass-panel").classList.toggle("hidden", !passed);
    $("fail-panel").classList.toggle("hidden", passed);

    if (passed) {
      $("pass-scenario-count").textContent = String(data.scenarios.length);
      $("pass-required").textContent = fmt(data.required_flow);
    } else {
      const f = data.failure;
      const isNormal = f.stage === "normal";
      $("fail-edge").textContent = isNormal
        ? "（正常网络本身）"
        : edgeLabel(f);
      $("fail-flow").textContent = fmt(f.max_flow);
      $("fail-required").textContent = fmt(f.required_flow);

      const cut = f.cut;
      renderChips($("cut-source-side"), cut.source_side_nodes, "src");
      renderChips($("cut-sink-side"), cut.sink_side_nodes, "sink");
      $("cut-edges-body").innerHTML = cutEdgeRows(cut, true);
      $("cut-capacity").textContent = fmt(cut.capacity);
    }

    // 正常网络
    const normal = data.normal;
    $("normal-flow").textContent = fmt(normal.max_flow);
    $("normal-required").textContent = fmt(data.required_flow);
    const meetsEl = $("normal-meets");
    meetsEl.textContent = normal.meets ? "达标" : "不达标";
    meetsEl.className = "metric-value " + (normal.meets ? "meets-yes" : "meets-no");
    renderChips($("normal-cut-source"), normal.cut.source_side_nodes, "src");
    renderChips($("normal-cut-sink"), normal.cut.sink_side_nodes, "sink");
    $("normal-cut-edges").innerHTML = cutEdgeRows(normal.cut, false);
    $("normal-cut-capacity").textContent = fmt(normal.cut.capacity);

    // 逐条失效
    const body = $("scenarios-body");
    body.innerHTML = "";
    if (data.scenarios.length === 0) {
      body.innerHTML = '<tr><td colspan="7" class="center tip">没有勾选“可检修”的管段，未模拟单段失效；仅审计正常网络。</td></tr>';
    } else {
      const failIdx = data.failure && !passed ? data.failure.edge_index : null;
      data.scenarios.forEach((s) => {
        const tr = document.createElement("tr");
        if (s.edge_index === failIdx) tr.className = "row-fail";
        const cells = [
          String(s.position),
          (s.edge_id ? s.edge_id + " " : "") + `${s.from} → ${s.to}`,
          "→",
          fmt(s.capacity),
          fmt(s.max_flow),
          fmt(data.required_flow),
        ];
        cells.forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        const tdJudge = document.createElement("td");
        const pill = document.createElement("span");
        pill.className = "pill " + (s.meets ? "yes" : "no");
        pill.textContent = s.meets ? "达标" : "不达标";
        tdJudge.appendChild(pill);
        tr.appendChild(tdJudge);
        body.appendChild(tr);
      });
    }

    const nonMaint = state.edges.filter((e) => !e.maintainable).length;
    const note = $("non-maintainable-note");
    if (nonMaint > 0) {
      note.classList.remove("hidden");
      note.textContent = `另有 ${nonMaint} 条未勾选“可检修”的管段，不参与单段临时失效模拟（视为事故期间保持投用）。`;
    } else {
      note.classList.add("hidden");
    }

    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRejection(err) {
    $("result-card").classList.add("hidden");
    $("plan-card").classList.add("hidden");
    const card = $("reject-card");
    card.classList.remove("hidden");
    $("reject-msg").textContent = err || "输入无效。";
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------------- 低暴露配流单渲染 ---------------- */

  function planScenarioTitle(p) {
    if (p.stage === "normal") return `情形 #${p.scenario} · 正常网络（无管段失效）`;
    const r = p.removed;
    const id = r.edge_id ? `（${r.edge_id}）` : "";
    return `情形 #${p.scenario} · 第 ${r.position} 条管段${id}临时失效：${r.from} → ${r.to}`;
  }

  function renderPlanScenario(p) {
    const wrap = document.createElement("div");
    wrap.className = "plan-scenario";

    const h = document.createElement("h3");
    h.textContent = planScenarioTitle(p);
    wrap.appendChild(h);

    const meta = document.createElement("p");
    meta.className = "tip";
    meta.textContent = `本情形分配流量 ${fmt(p.flow_value)}（＝事故必须持续排出量），最低总代价 `;
    const strong = document.createElement("strong");
    strong.textContent = fmt(p.total_cost);
    meta.appendChild(strong);
    wrap.appendChild(meta);

    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const table = document.createElement("table");
    table.className = "plan-table";
    table.innerHTML =
      "<thead><tr><th>#</th><th>管段编号</th><th>方向</th><th>容量上限</th>" +
      "<th>单位暴露代价</th><th>分配流量</th><th>暴露代价</th></tr></thead>";
    const tbody = document.createElement("tbody");
    p.flows.forEach((f) => {
      const tr = document.createElement("tr");
      if (f.removed) tr.className = "row-removed";
      const dir = `${f.from} → ${f.to}`;
      const cells = [
        String(f.position),
        (f.edge_id || "—") + (f.removed ? "（已移除）" : ""),
        dir,
        fmt(f.capacity),
        fmt(f.cost),
        fmt(f.flow),
        fmt(f.exposure),
      ];
      cells.forEach((c) => {
        const td = document.createElement("td");
        td.textContent = c;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    const tfoot = document.createElement("tfoot");
    tfoot.innerHTML =
      `<tr><td colspan="6" class="right">情形总代价（Σ 流量 × 单位暴露代价）</td>` +
      `<td class="strong">${fmt(p.total_cost)}</td></tr>`;
    table.appendChild(tfoot);
    scroll.appendChild(table);
    wrap.appendChild(scroll);
    return wrap;
  }

  function renderPlan(data) {
    const card = $("plan-card");
    card.classList.remove("hidden");
    $("plan-stale-banner").classList.add("hidden");

    const blocked = !data.passed;
    $("plan-blocked").classList.toggle("hidden", !blocked);
    $("plan-summary").classList.toggle("hidden", blocked);
    if (blocked) {
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const plans = data.plans || [];
    $("plan-required").textContent = fmt(data.required_flow);
    $("plan-count").textContent = String(plans.length);
    const normal = plans.find((p) => p.stage === "normal");
    $("plan-normal-cost").textContent = normal ? fmt(normal.total_cost) : "—";

    const box = $("plan-scenarios");
    box.innerHTML = "";
    plans.forEach((p) => box.appendChild(renderPlanScenario(p)));
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------------- 提交审计（真实业务 API） ---------------- */

  async function submitAudit() {
    const payload = currentPayload();
    $("btn-audit").disabled = true;
    $("draft-hint").textContent = "正在调用服务端审计 API…";
    try {
      const resp = await fetch("/api/audit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        renderRejection(data.error || `审计请求失败（HTTP ${resp.status}）`);
        state.lastAuditSignature = null;
        return;
      }
      state.lastAuditSignature = signature();
      renderResult(data);
    } catch (e) {
      renderRejection("无法连接审计服务：" + e.message);
      state.lastAuditSignature = null;
    } finally {
      $("btn-audit").disabled = false;
      if ($("draft-hint").textContent.startsWith("正在")) $("draft-hint").textContent = "";
    }
  }

  /* ---------------- 生成低暴露配流单（真实业务 API） ---------------- */

  async function submitPlan() {
    const payload = planPayload();
    $("btn-plan").disabled = true;
    $("draft-hint").textContent = "正在调用服务端配流 API…";
    try {
      const resp = await fetch("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        renderRejection(data.error || `配流请求失败（HTTP ${resp.status}）`);
        state.lastPlanSignature = null;
        return;
      }
      // 服务端已按既有规则重新审计当前草稿：同步刷新审计结论
      // （不放行时此处即展示首条失效管段与割集证据），并渲染配流单。
      state.lastAuditSignature = signature();
      state.lastPlanSignature = planSignature();
      renderResult(data.audit);
      renderPlan(data);
    } catch (e) {
      renderRejection("无法连接配流服务：" + e.message);
      state.lastPlanSignature = null;
    } finally {
      $("btn-plan").disabled = false;
      if ($("draft-hint").textContent.startsWith("正在")) $("draft-hint").textContent = "";
    }
  }

  /* ---------------- 载入 / 清空 ---------------- */

  function loadExample(ex) {
    clearResult();
    $("in-source").value = ex.source;
    $("in-sink").value = ex.sink;
    $("in-required").value = String(ex.required_flow);
    state.nodes = ex.nodes.slice();
    state.edges = ex.edges.map((e) => ({ ...e }));
    state.lastAuditSignature = null;
    state.lastPlanSignature = null;
    renderAll();
  }

  function clearAll() {
    clearResult();
    $("in-source").value = "";
    $("in-sink").value = "";
    $("in-required").value = "";
    state.nodes = [];
    state.edges = [];
    state.lastAuditSignature = null;
    state.lastPlanSignature = null;
    renderAll();
  }

  /* ---------------- 事件绑定 ---------------- */

  $("btn-add-node").addEventListener("click", () => {
    state.nodes.push("");
    renderNodes();
    markDirty();
    const inputs = $("nodes-list").querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  });

  $("btn-add-edge").addEventListener("click", () => {
    state.edges.push({ id: "", from: "", to: "", capacity: "", cost: "", maintainable: true });
    renderEdges();
    markDirty();
  });

  $("btn-audit").addEventListener("click", submitAudit);
  $("btn-plan").addEventListener("click", submitPlan);
  $("btn-example-pass").addEventListener("click", () => loadExample(EXAMPLE_PASS));
  $("btn-example-fail").addEventListener("click", () => loadExample(EXAMPLE_FAIL));
  $("btn-clear").addEventListener("click", clearAll);
  ["in-source", "in-sink", "in-required"].forEach((id) =>
    $(id).addEventListener("input", markDirty)
  );

  renderAll();
})();
