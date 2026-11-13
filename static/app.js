(() => {
  "use strict";

  const state = {
    datasets: [],
    sides: {
      left: { dataset: null, columns: [], keyFields: [], secondaryFields: [], dedup: false, nullPolicy: {} },
      right: { dataset: null, columns: [], keyFields: [], secondaryFields: [], dedup: false, nullPolicy: {} },
    },
    plan: null,          // 最近一次成功预览的统计
    config: null,        // 该预览对应的配置
    stale: false,        // 配置已变、预览已过期
    baseline: null,      // 上一次预览，用于规则对比
    executed: false,
  };

  const el = (id) => document.getElementById(id);
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString("zh-CN") : String(n ?? "—"));
  const pct = (n) => `${(n * 100).toFixed(1)}%`;

  // ---------------------------------------------------------------- 请求
  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let body = {};
    try { body = await res.json(); } catch (_) { /* 忽略非 JSON */ }
    if (!res.ok) throw new Error(body.error || `请求失败（${res.status}）`);
    return body;
  }

  function toast(message, kind = "ok") {
    const node = el("toast");
    node.textContent = message;
    node.className = `toast ${kind}`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.add("hidden"), 3600);
  }

  // ---------------------------------------------------------------- 数据集
  async function loadDatasets() {
    try {
      const data = await api("/api/datasets");
      state.datasets = data.datasets;
      renderDatasetSelects();
      if (state.datasets.length >= 2) {
        selectDataset("left", state.datasets[0].id);
        selectDataset("right", state.datasets[1].id);
      }
      setServerState("就绪", "idle");
    } catch (err) {
      setServerState("离线", "danger");
      toast(err.message, "danger");
    }
  }

  function renderDatasetSelects() {
    for (const side of ["left", "right"]) {
      const select = el(`${side}Dataset`);
      select.innerHTML = state.datasets
        .map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.name)} · ${d.rows} 行</option>`)
        .join("");
      select.onchange = () => selectDataset(side, select.value);
    }
  }

  function selectDataset(side, id) {
    const dataset = state.datasets.find((d) => d.id === id);
    if (!dataset) return;
    const target = state.sides[side];
    target.dataset = id;
    target.columns = [...dataset.columns];
    target.keyFields = guessKeys(dataset.columns).slice(0, 1);
    target.secondaryFields = [];
    target.nullPolicy = {};
    for (const col of target.columns) target.nullPolicy[col] = false;
    renderSide(side);
    // 换表后旧预览不再代表当前数据：直接作废，避免误执行到另一张表。
    state.plan = null;
    state.config = null;
    state.baseline = null;
    state.stale = false;
    el("statsBlock").classList.add("hidden");
    el("emptyState").classList.remove("hidden");
    el("previewState").className = "pill pill-idle";
    el("previewState").textContent = "尚未预览";
    el("execNotice").classList.add("hidden");
    el("execPreview").classList.add("hidden");
    el("confirmCheck").checked = false;
    el("execState").className = "pill pill-idle";
    el("execState").textContent = "未执行";
    syncExecuteControls();
  }

  function guessKeys(columns) {
    const preferred = columns.filter((c) =>
      /^(id|key|code|region|sku|order|pay|account|customer)/i.test(c));
    return (preferred.length ? preferred : columns).slice(0, 4);
  }

  function renderSide(side) {
    const target = state.sides[side];
    const dataset = state.datasets.find((d) => d.id === target.dataset);
    if (dataset) {
      el(`${side}Columns`).innerHTML = dataset.columns
        .map((c) => `<span class="chip">${escapeHtml(c)}</span>`).join("");
      if (el(`${side}Dataset`).value !== target.dataset) el(`${side}Dataset`).value = target.dataset;
    }

    el(`${side}Keys`).innerHTML = target.columns.map((col) => {
      const active = target.keyFields.includes(col);
      return `<button type="button" class="key-chip" data-field="${escapeHtml(col)}" aria-pressed="${active}">${escapeHtml(col)}</button>`;
    }).join("");
    el(`${side}Keys`).querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => {
        const field = btn.dataset.field;
        const idx = target.keyFields.indexOf(field);
        if (idx >= 0) target.keyFields.splice(idx, 1);
        else if (target.keyFields.length < 4) target.keyFields.push(field);
        else toast("最多选择 4 个关联字段", "danger");
        renderSide(side);
        invalidatePreview();
      };
    });

    const secondaryOptions = target.columns.filter((c) => !target.keyFields.includes(c));
    el(`${side}Secondary`).innerHTML = secondaryOptions.map((col) => {
      const active = target.secondaryFields.includes(col);
      return `<button type="button" class="key-chip" data-field="${escapeHtml(col)}" aria-pressed="${active}">${escapeHtml(col)}</button>`;
    }).join("") || `<span class="muted">选择关联字段后可添加次级字段</span>`;
    el(`${side}Secondary`).querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => {
        const field = btn.dataset.field;
        const idx = target.secondaryFields.indexOf(field);
        if (idx >= 0) target.secondaryFields.splice(idx, 1);
        else target.secondaryFields.push(field);
        renderSide(side);
        invalidatePreview();
      };
    });

    const nullFields = [...target.keyFields, ...target.secondaryFields];
    el(`${side}Null`).innerHTML = nullFields.map((col) => `
      <div class="null-row">
        <span>${escapeHtml(col)} 的空值</span>
        <select data-field="${escapeHtml(col)}">
          <option value="false"${target.nullPolicy[col] ? "" : " selected"}>视为未匹配</option>
          <option value="true"${target.nullPolicy[col] ? " selected" : ""}>参与匹配</option>
        </select>
      </div>`).join("") || `<span class="muted">选择关联字段后设置空值规则</span>`;
    el(`${side}Null`).querySelectorAll("select").forEach((sel) => {
      sel.onchange = () => {
        target.nullPolicy[sel.dataset.field] = sel.value === "true";
        invalidatePreview();
      };
    });

    const dedup = el(`${side}Dedup`);
    dedup.checked = target.dedup;
    dedup.onchange = () => { target.dedup = dedup.checked; invalidatePreview(); };
  }

  // ---------------------------------------------------------------- 预览
  function buildPayload() {
    const side = (name) => ({
      dataset: state.sides[name].dataset,
      key_fields: state.sides[name].keyFields,
      secondary_fields: state.sides[name].secondaryFields,
      dedup_mode: state.sides[name].dedup ? "key" : "none",
      null_field_override: state.sides[name].nullPolicy,
    });
    return {
      left: side("left"),
      right: side("right"),
      join_type: el("joinType").value,
      max_output_rows: Number(el("maxRows").value) || 200000,
    };
  }

  function validateConfig() {
    for (const side of ["left", "right"]) {
      if (!state.sides[side].keyFields.length) {
        toast(`请为${side === "left" ? "左" : "右"}表选择关联字段`, "danger");
        return false;
      }
    }
    return true;
  }

  function invalidatePreview() {
    // 配置改变：已有预览标记为过期，执行必须重新预览确认；旧预览保留为对比基线。
    if (!state.plan) {
      el("previewState").className = "pill pill-idle";
      el("previewState").textContent = "尚未预览";
      return;
    }
    state.stale = true;
    state.executed = false;
    el("previewState").className = "pill pill-warn";
    el("previewState").textContent = "规则已变，请重新预览";
    el("confirmCheck").checked = false;
    syncExecuteControls();
  }

  async function preview() {
    if (!validateConfig()) return;
    const payload = buildPayload();
    el("previewBtn").disabled = true;
    el("previewBtn").textContent = "计算中…";
    try {
      const data = await api("/api/preview", { method: "POST", body: JSON.stringify(payload) });
      // 上一次预览作为规则对比基线；首次预览没有基线。
      if (state.plan) {
        state.baseline = { stats: state.plan, config: state.config };
      }
      state.plan = data.stats;
      state.config = payload;
      state.stale = false;
      state.executed = false;
      renderStats(data.stats, data);
      el("confirmCheck").checked = false;
      syncExecuteControls();
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      el("previewBtn").disabled = false;
      el("previewBtn").textContent = "预览关联";
    }
  }

  function renderStats(stats, data) {
    el("emptyState").classList.add("hidden");
    el("statsBlock").classList.remove("hidden");

    const r = stats.rows;
    const limit = Number(el("maxRows").value) || 200000;
    el("statResult").textContent = fmt(r.result);
    el("statResultHint").textContent = r.result > limit
      ? `超过上限 ${fmt(limit)} 行，执行时只会得到受限预览`
      : `左 ${fmt(r.left)} / 右 ${fmt(r.right)} 行 → ${fmt(r.matched)} 匹配行 + 未匹配`;
    el("previewState").className = r.result > limit ? "pill pill-warn" : "pill pill-ok";
    el("previewState").textContent = r.result > limit ? "超出上限" : "预览已完成";

    const cardMap = {
      one_to_one: ["一对一", "每个键在两侧各出现一次，不放大行数"],
      one_to_many: ["一对多", "至少一侧键重复，部分键会放大行数"],
      many_to_many: ["多对多", "两侧键都有重复，行数可能成倍膨胀"],
    };
    const [label, hint] = cardMap[stats.keys.cardinality] || ["—", ""];
    el("statCardinality").textContent = label;
    el("statCardinalityHint").textContent = hint;

    const u = stats.unmatched;
    el("statLeftUnmatched").textContent = fmt(u.left);
    el("statLeftUnmatchedHint").textContent = `其中 ${fmt(u.left_null_key)} 行因空值规则被排除`;
    el("statRightUnmatched").textContent = fmt(u.right);
    el("statRightUnmatchedHint").textContent = `其中 ${fmt(u.right_null_key)} 行因空值规则被排除`;

    renderUniqueness(stats);
    renderFieldPairs(stats);
    renderBreakdown(stats.keys.breakdown);
    renderTopKeys(stats);
    renderUnmatched("unmatchedLeft", stats.samples.unmatched_left);
    renderUnmatched("unmatchedRight", stats.samples.unmatched_right);
    if (state.baseline) {
      el("compareBlock").classList.remove("hidden");
      renderCompare(state.baseline.stats, stats);
    } else {
      el("compareBlock").classList.add("hidden");
    }
    renderExecuteHint(stats);
  }

  function renderUniqueness(stats) {
    const row = (side, info) => {
      const tag = info.is_unique
        ? `<span class="tag ok">唯一</span>`
        : `<span class="tag ${info.duplicate_keys ? "danger" : "warn"}">${info.duplicate_keys} 个键重复</span>`;
      return `<tr>
        <td>${side}</td>
        <td class="num">${fmt(info.kept_rows)}</td>
        <td class="num">${fmt(info.distinct_keys)}</td>
        <td class="num">${fmt(info.unique_keys)}</td>
        <td class="num">${pct(info.unique_rate)}</td>
        <td>${tag}</td>
      </tr>`;
    };
    el("uniqueTable").innerHTML = `
      <table>
        <thead><tr><th>侧</th><th class="num">参与匹配行</th><th class="num">不同键</th><th class="num">唯一键</th><th class="num">唯一率</th><th>判定</th></tr></thead>
        <tbody>${row("左表", stats.keys.left)}${row("右表", stats.keys.right)}</tbody>
      </table>`;
  }

  function renderFieldPairs(stats) {
    const pairs = stats.field_pairs || [];
    const html = pairs.map((p) => {
      const warn = p.same_name ? "" : ` <span class="tag warn">名称不同</span>`;
      return `<div class="pair-row"><span class="mono">${escapeHtml(p.left)}</span><span class="muted">↔</span><span class="mono">${escapeHtml(p.right)}</span>${warn}</div>`;
    }).join("");
    el("fieldPairs").innerHTML = html || `<span class="muted">未选择字段</span>`;
  }

  function renderBreakdown(breakdown) {
    const entries = [
      ["一对一", breakdown.one_to_one, false],
      ["一对多", breakdown.one_to_many, true],
      ["多对一", breakdown.many_to_one, true],
      ["多对多", breakdown.many_to_many, true],
    ];
    const max = Math.max(1, ...entries.map(([, n]) => n));
    el("cardinalityBreakdown").innerHTML = entries.map(([label, value, warn]) => `
      <div class="bar-row">
        <span>${label}</span>
        <div class="bar-track"><div class="bar-fill ${warn && value ? "warn" : ""}" style="width:${(value / max) * 100}%"></div></div>
        <span class="num">${fmt(value)}</span>
      </div>`).join("");
  }

  function renderTopKeys(stats) {
    const keys = stats.expansion.top_keys || [];
    if (!keys.length) {
      el("topKeys").innerHTML = `<p class="muted">没有匹配键；两侧没有任何键重合。</p>`;
      el("keyDetail").classList.add("hidden");
      return;
    }
    const rows = keys.map((k, index) => {
      const danger = k.many_to_many ? "danger" : (k.inflates ? "warn" : "");
      return `<tr class="key-row" data-index="${index}">
        <td class="mono">${escapeHtml(k.key.label)}</td>
        <td class="num">${fmt(k.left_count)}</td>
        <td class="num">${fmt(k.right_count)}</td>
        <td class="num ${danger}">${fmt(k.output_rows)}</td>
        <td>${k.many_to_many ? `<span class="tag danger">多对多</span>` : (k.inflates ? `<span class="tag warn">膨胀</span>` : `<span class="tag">正常</span>`)}</td>
        <td><button class="btn btn-ghost" type="button" data-expand="${index}">展开</button></td>
      </tr>`;
    }).join("");
    el("topKeys").innerHTML = `
      <table>
        <thead><tr><th>键</th><th class="num">左频次</th><th class="num">右频次</th><th class="num">产出行数</th><th>性质</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    el("topKeys").querySelectorAll("[data-expand]").forEach((btn) => {
      btn.onclick = (ev) => {
        ev.stopPropagation();
        expandKey(keys[Number(btn.dataset.expand)], btn);
      };
    });
    el("topKeys").querySelectorAll("tr.key-row").forEach((tr) => {
      tr.onclick = () => {
        const k = keys[Number(tr.dataset.index)];
        expandKey(k, tr.querySelector("[data-expand]"));
      };
    });
  }

  async function expandKey(key, trigger) {
    el("topKeys").querySelectorAll("tr.key-row").forEach((tr) => tr.classList.remove("active"));
    const row = trigger.closest("tr.key-row");
    if (row) row.classList.add("active");
    try {
      const data = await api("/api/key-detail", {
        method: "POST",
        body: JSON.stringify({ ...state.config, key_raw: key.key_raw }),
      });
      renderKeyDetail(data);
    } catch (err) {
      toast(err.message, "danger");
    }
  }

  function renderKeyDetail(detail) {
    const columnsOf = (rows) => {
      if (!rows.length) return [];
      return Object.keys(rows[0]).filter((c) => c !== "row");
    };
    const tableFor = (rows, emptyText) => {
      if (!rows.length) return `<p class="muted">${emptyText}</p>`;
      const cols = columnsOf(rows);
      return `<table>
        <thead><tr><th>行号</th>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((r) => `<tr><td class="num">${r.row}</td>${cols.map((c) => `<td>${escapeHtml(r[c])}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>`;
    };
    const note = (truncated, total) => truncated
      ? `<p class="muted">仅展示前 ${fmt(total)} 行中的部分，完整为 ${fmt(total)} 行</p>` : "";
    el("keyDetail").classList.remove("hidden");
    el("keyDetail").innerHTML = `
      <h4>键 ${escapeHtml(detail.key.label)}</h4>
      <p class="muted">左 ${fmt(detail.left_count)} 行 × 右 ${fmt(detail.right_count)} 行 = 产出 ${fmt(detail.output_rows)} 行</p>
      <div class="detail-grid">
        <div><h5>左表记录</h5><div class="detail-scroll">${tableFor(detail.left_rows, "左表没有该键的记录")}</div>
          ${note(detail.left_truncated, detail.left_total)}</div>
        <div><h5>右表记录</h5><div class="detail-scroll">${tableFor(detail.right_rows, "右表没有该键的记录")}</div>
          ${note(detail.right_truncated, detail.right_total)}</div>
      </div>`;
  }

  function renderUnmatched(id, rows) {
    if (!rows.length) {
      el(id).innerHTML = `<p class="muted">没有未匹配记录。</p>`;
      return;
    }
    const cols = Object.keys(rows[0]).filter((c) => c !== "row");
    el(id).innerHTML = `<table>
      <thead><tr><th>行号</th>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr><td class="num">${r.row}</td>${cols.map((c) => `<td>${escapeHtml(r[c])}</td>`).join("")}</tr>`).join("")}</tbody>
    </table>`;
  }

  function renderCompare(previous, current) {
    const rows = [
      ["结果行数", previous.rows.result, current.rows.result],
      ["匹配行数", previous.rows.matched, current.rows.matched],
      ["左表未匹配", previous.rows.left_unmatched, current.rows.left_unmatched],
      ["右表未匹配", previous.rows.right_unmatched, current.rows.right_unmatched],
      ["左表重复键", previous.keys.left.duplicate_keys, current.keys.left.duplicate_keys],
      ["右表重复键", previous.keys.right.duplicate_keys, current.keys.right.duplicate_keys],
      ["去重丢弃行（左）", previous.dedup.left_dropped, current.dedup.left_dropped],
      ["去重丢弃行（右）", previous.dedup.right_dropped, current.dedup.right_dropped],
    ].map(([label, before, after]) => {
      const delta = after - before;
      const kind = delta === 0 ? "" : (Math.abs(delta) > 0 ? "warn" : "");
      const text = delta === 0 ? "无变化" : `${delta > 0 ? "+" : ""}${fmt(delta)}`;
      return `<tr><td>${label}</td><td class="num">${fmt(before)}</td><td class="num">${fmt(after)}</td><td class="num ${kind}">${text}</td></tr>`;
    }).join("");
    el("compareTable").innerHTML = `<table>
      <thead><tr><th>指标</th><th class="num">上次预览</th><th class="num">当前预览</th><th class="num">变化</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function renderExecuteHint(stats) {
    const limit = Number(el("maxRows").value) || 200000;
    const notice = el("execNotice");
    notice.classList.remove("hidden");
    if (stats.rows.result > limit) {
      notice.className = "notice warn";
      notice.textContent = `完整结果 ${fmt(stats.rows.result)} 行，超过上限 ${fmt(limit)} 行。执行将只返回受限预览，不会静默丢行；如确需文件，可授权截断写出。`;
    } else {
      notice.className = "notice";
      notice.textContent = `执行后将写出完整结果 ${fmt(stats.rows.result)} 行，并保存本次使用的文件指纹、关联条件与处理规则。`;
    }
    el("execPreview").classList.add("hidden");
  }

  // ---------------------------------------------------------------- 执行
  function syncExecuteControls() {
    const previewed = Boolean(state.plan) && !state.stale;
    el("confirmCheck").disabled = !previewed;
    el("executeBtn").disabled = !(previewed && el("confirmCheck").checked);
  }

  async function execute() {
    if (!state.plan || state.stale) {
      toast("配置已变更，请先重新预览", "danger");
      return;
    }
    const payload = { ...state.config, confirmed: true };
    const name = el("outputName").value.trim();
    if (name) payload.output_name = name;
    const limit = Number(el("maxRows").value) || 200000;
    payload.max_output_rows = limit;
    const overLimit = state.plan.rows.result > limit;
    if (overLimit) {
      const ok = window.confirm(
        `完整结果 ${state.plan.rows.result.toLocaleString("zh-CN")} 行，超过上限。\n` +
        "点击“确定”将写出前若干行并在记录中标记为截断；点击“取消”则不写文件。"
      );
      if (!ok) {
        payload.allow_truncate = false;
      } else {
        payload.allow_truncate = true;
      }
    }
    el("executeBtn").disabled = true;
    el("executeBtn").textContent = "执行中…";
    try {
      const data = await api("/api/execute", { method: "POST", body: JSON.stringify(payload) });
      renderExecution(data);
      state.executed = true;
      loadRuns();
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      el("executeBtn").textContent = "执行关联";
      syncExecuteControls();
    }
  }

  function renderExecution(data) {
    const notice = el("execNotice");
    const kind = data.status === "completed" ? "ok" : (data.status === "partial" ? "warn" : "danger");
    notice.className = `notice ${kind}`;
    notice.classList.remove("hidden");
    const path = data.output_path ? `<div class="mono">${escapeHtml(data.output_path)}</div>` : "";
    const audit = data.audit && data.audit.run_id
      ? `<div class="muted">审计记录：<span class="mono">${escapeHtml(data.audit.run_id)}</span></div>` : "";
    notice.innerHTML = `<strong>${statusLabel(data.status)}</strong>：${escapeHtml(data.note)}${path}${audit}`;

    el("execState").className = `pill ${kind === "ok" ? "pill-ok" : kind === "warn" ? "pill-warn" : "pill-danger"}`;
    el("execState").textContent = statusLabel(data.status);

    if (data.preview && data.preview.length) {
      el("execPreview").classList.remove("hidden");
      const cols = data.columns.filter((c) => c !== "_key");
      el("execPreview").innerHTML = `<table>
        <thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
        <tbody>${data.preview.map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c])}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>`;
    }
    toast(data.note, data.status === "completed" ? "ok" : "danger");
  }

  function statusLabel(status) {
    return { completed: "执行完成", partial: "已截断写出", limited: "受限预览，未写出" }[status] || status;
  }

  // ---------------------------------------------------------------- 审计
  async function loadRuns() {
    try {
      const data = await api("/api/runs");
      renderRuns(data.runs);
    } catch (err) {
      el("runsTable").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    }
  }

  function renderRuns(runs) {
    if (!runs.length) {
      el("runsTable").innerHTML = `<p class="muted">暂无执行记录。执行关联后，这里会列出每次使用的文件、条件与处理规则。</p>`;
      return;
    }
    el("runsTable").innerHTML = `<table>
      <thead><tr><th>运行</th><th>时间</th><th>方式</th><th class="num">完整行数</th><th class="num">写出</th><th>状态</th></tr></thead>
      <tbody>${runs.map((r) => `<tr class="run-row" data-run="${escapeHtml(r.run_id)}">
        <td class="mono">${escapeHtml(r.run_id)}</td>
        <td>${escapeHtml(r.created_at || "")}</td>
        <td>${escapeHtml(r.join_type || "")}</td>
        <td class="num">${fmt(r.full_rows)}</td>
        <td class="num">${fmt(r.written_rows)}</td>
        <td>${r.truncated ? `<span class="tag warn">截断</span>` : `<span class="tag ok">完整</span>`}</td>
      </tr>`).join("")}</tbody></table>`;
    el("runsTable").querySelectorAll(".run-row").forEach((tr) => {
      tr.onclick = () => showRun(tr.dataset.run);
    });
  }

  async function showRun(runId) {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}`);
      const run = data.run;
      el("runDetail").classList.remove("hidden");
      el("runDetail").innerHTML = `
        <h4>运行 ${escapeHtml(run.run_id)} · ${escapeHtml(run.created_at || "")}</h4>
        <pre class="mono" style="white-space:pre-wrap;margin:0">${escapeHtml(JSON.stringify(run, null, 2))}</pre>`;
    } catch (err) {
      toast(err.message, "danger");
    }
  }

  // ---------------------------------------------------------------- 工具
  function setServerState(text, kind) {
    const node = el("serverState");
    node.textContent = text;
    node.className = `pill pill-${kind}`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    el("previewBtn").onclick = preview;
    el("confirmCheck").onchange = syncExecuteControls;
    el("maxRows").onchange = () => {
      if (state.plan) renderExecuteHint(state.plan);
    };
    el("joinType").onchange = invalidatePreview;
    el("executeBtn").onclick = execute;
    el("refreshRuns").onclick = loadRuns;
    el("reloadBtn").onclick = () => window.location.reload();
  }

  bind();
  loadDatasets();
  loadRuns();
})();
