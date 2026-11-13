/**
 * 最小 DOM 垫片：让真实的 static/app.js 能在 Node 中运行。
 * 只实现 app.js 实际使用的 API（getElementById / innerHTML / classList /
 * querySelectorAll / closest / dataset / fetch 等）。
 */
"use strict";

const fs = require("fs");
const path = require("path");

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return this.el._classes; }
  add(...names) { names.forEach((n) => this.el._classes.add(n)); this.el._syncClass(); }
  remove(...names) { names.forEach((n) => this.el._classes.delete(n)); this.el._syncClass(); }
  contains(name) { return this.el._classes.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name); else this.remove(name);
    return on;
  }
}

function unescapeHtml(text) {
  return text
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

function parseAttributes(raw) {
  const attrs = {};
  const re = /([a-zA-Z0-9_\-:.]+)\s*=\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(raw))) attrs[m[1]] = unescapeHtml(m[2]);
  return attrs;
}

class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toLowerCase();
    this.attrs = attrs;
    this._classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    this.children = [];
    this.parentNode = null;
    this._text = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.onclick = null;
    this.onchange = null;
    this.classList = new ClassList(this);
    this.dataset = {};
    for (const [key, value] of Object.entries(attrs)) {
      if (key.startsWith("data-")) {
        this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
      }
    }
    this.id = attrs.id || "";
  }

  _syncClass() { this.attrs.class = [...this._classes].join(" "); }

  get className() { return this.attrs.class || ""; }
  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    this.attrs.class = String(value);
  }

  get textContent() {
    let text = this._text;
    for (const child of this.children) text += child.textContent;
    return text;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  set innerHTML(html) {
    this.children = [];
    this._text = "";
    if (!html) return;
    const tokenRe = /<(\/?)([a-zA-Z][a-zA-Z0-9_-]*)([^>]*)>/g;
    let lastIndex = 0;
    let current = this;
    const stack = [];
    let m;
    while ((m = tokenRe.exec(html))) {
      const [full, closing, tag, rawAttrs] = m;
      const between = html.slice(lastIndex, m.index);
      if (between.trim()) {
        current._text += unescapeHtml(between.trim());
      }
      lastIndex = m.index + full.length;
      if (closing) {
        if (stack.length) stack.pop();
        current = stack.length ? stack[stack.length - 1] : this;
        continue;
      }
      if (rawAttrs.trim().endsWith("/")) {
        const child = new El(tag, parseAttributes(rawAttrs));
        child.parentNode = current;
        current.children.push(child);
        continue;
      }
      const child = new El(tag, parseAttributes(rawAttrs));
      child.parentNode = current;
      current.children.push(child);
      stack.push(child);
      current = child;
    }
    const tail = html.slice(lastIndex);
    if (tail.trim()) this._text += unescapeHtml(tail.trim());
  }

  _walk(fn) {
    for (const child of this.children) {
      fn(child);
      child._walk(fn);
    }
  }

  querySelectorAll(selector) {
    const matches = [];
    const test = compile(selector);
    this._walk((node) => { if (test(node)) matches.push(node); });
    return matches;
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }

  closest(selector) {
    const test = compile(selector);
    let node = this;
    while (node) {
      if (node !== this && test(node)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

function compile(selector) {
  selector = selector.trim();
  if (selector.startsWith("[")) {
    const name = selector.slice(1, -1);
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return (el) => el.dataset[key] !== undefined || el.attrs[name] !== undefined;
    }
    return (el) => el.attrs[name] !== undefined;
  }
  const classMatch = selector.match(/(?:([a-zA-Z][a-zA-Z0-9_-]*)?)\.([a-zA-Z0-9_-]+)$/);
  if (classMatch) {
    const [, tag, cls] = classMatch;
    return (el) => (!tag || el.tagName === tag.toLowerCase()) && el.classList.contains(cls);
  }
  return (el) => el.tagName === selector.toLowerCase();
}

const ID_TAGS = {};

function makeDocument(htmlPath, idTags) {
  Object.assign(ID_TAGS, idTags);
  const map = new Map();
  for (const [id, tag] of Object.entries(ID_TAGS)) {
    const el = new El(tag || "div", { id });
    map.set(id, el);
  }
  return {
    getElementById(id) { return map.get(id) || null; },
    map,
  };
}

// ---------------------------------------------------------------- 测试驱动
async function main() {
  const root = path.resolve(__dirname, "..");
  const html = fs.readFileSync(path.join(root, "static/index.html"), "utf8");
  const idTags = {};
  for (const m of html.matchAll(/<([a-zA-Z][a-zA-Z0-9_-]*)[^>]*\sid="([^"]+)"/g)) {
    idTags[m[2]] = m[1];
  }

  const document = makeDocument(html, idTags);
  const requests = [];
  const responses = {};

  function jsonResponse(status, body) {
    return { ok: status < 400, status, json: async () => body };
  }

  async function routeFetch(url, options) {
    requests.push({ url, body: options && options.body ? JSON.parse(options.body) : null });
    const handler = responses[url.split("?")[0]];
    if (!handler) return jsonResponse(404, { error: "no stub" });
    const { status, body } = handler(options && options.body ? JSON.parse(options.body) : {});
    return jsonResponse(status || 200, body);
  }

  const sandbox = {
    document,
    window: { confirm: () => true, location: { reload: () => {} } },
    fetch: routeFetch,
    setTimeout, clearTimeout, console,
    URL, URLSearchParams,
    escapeHtml: undefined,
  };
  const el = (id) => document.getElementById(id);
  const settle = () => new Promise((r) => setTimeout(r, 30));
  const assert = (cond, message) => {
    if (!cond) throw new Error("断言失败：" + message);
  };

  // ---- 数据集接口
  responses["/api/datasets"] = () => ({
    body: {
      datasets: [
        { id: "orders", name: "orders", columns: ["order_id", "region", "code", "amount", "customer"], rows: 9 },
        { id: "payments", name: "payments", columns: ["pay_id", "region", "code", "amount", "memo"], rows: 10 },
      ],
    },
  });

  const stats = {
    join_type: "inner", join_label: "内连接",
    fields: { left: ["region", "code"], right: ["region", "code"] },
    rows: { left: 9, right: 10, matched: 13, left_unmatched: 2, right_unmatched: 2, result: 13, matched_keys: 5 },
    field_pairs: [{ position: 1, left: "region", right: "region", same_name: true }, { position: 2, left: "code", right: "code", same_name: true }],
    keys: { matched: 5, cardinality: "many_to_many",
      breakdown: { one_to_one: 3, one_to_many: 0, many_to_one: 0, many_to_many: 2 },
      left: { kept_rows: 8, distinct_keys: 6, unique_keys: 4, duplicate_keys: 2, unique_rate: 0.666, is_unique: false },
      right: { kept_rows: 9, distinct_keys: 6, unique_keys: 4, duplicate_keys: 2, unique_rate: 0.666, is_unique: false } },
    unmatched: { left: 2, right: 2, left_null_key: 1, right_null_key: 1 },
    dedup: { left_mode: "none", right_mode: "none", left_dropped: 0, right_dropped: 0 },
    expansion: { extra_rows: 4, many_to_many_keys: 2, top_keys: [
      { key: { label: "region=south · code=C", values: { region: "south", code: "C" }, fields: ["region", "code"] }, key_raw: [["v", "south"], ["v", "C"]], left_count: 2, right_count: 3, output_rows: 6, extra_rows: 5, inflates: true, many_to_many: true },
      { key: { label: "region=west · code=E", values: { region: "west", code: "E" }, fields: ["region", "code"] }, key_raw: [["v", "west"], ["v", "E"]], left_count: 2, right_count: 2, output_rows: 4, extra_rows: 3, inflates: true, many_to_many: true },
    ] },
    samples: { unmatched_left: [{ row: 6, region: "east", code: "D" }], unmatched_right: [{ row: 110, region: "central", code: "F" }] },
  };
  responses["/api/preview"] = () => ({ body: { stats, columns: { left: [], right: [] }, sources: {} } });

  let detailCalls = 0;
  responses["/api/key-detail"] = (body) => {
    detailCalls += 1;
    return {
      body: {
        key: { label: "region=south · code=C", fields: ["region", "code"], values: { region: "south", code: "C" } },
        left_count: 2, right_count: 3, output_rows: 6,
        left_rows: [{ row: 4, region: "south", code: "C", amount: "300" }, { row: 5, region: "south", code: "C", amount: "300" }],
        right_rows: [{ row: 4, region: "south", code: "C", amount: "300" }, { row: 5, region: "south", code: "C", amount: "300" }, { row: 6, region: "south", code: "C", amount: "300" }],
        left_truncated: false, right_truncated: false, left_total: 2, right_total: 3,
      },
    };
  };
  responses["/api/execute"] = () => ({ status: 200, body: {
    status: "completed", note: "已写出完整结果 13 行。", full_rows: 13, written_rows: 13,
    truncated: false, columns: ["_left_row", "_right_row", "_key", "left.order_id", "right.pay_id"],
    preview: [{ _left_row: 1, _right_row: 1, _key: "region=south · code=C", "left.order_id": "1", "right.pay_id": "101" }],
    output_path: "/tmp/out.csv", audit: { run_id: "20260101-000000-abcd" }, stats,
  } });
  responses["/api/runs"] = () => ({ body: { runs: [
    { run_id: "20260101-000000-abcd", created_at: "2026-01-01T00:00:00", status: "completed",
      join_type: "inner", full_rows: 13, written_rows: 13, truncated: false, left: "orders", right: "payments", path: "/tmp/a.json" },
  ] } });
  responses["/api/runs/20260101-000000-abcd"] = () => ({ body: { run: {
    run_id: "20260101-000000-abcd", created_at: "2026-01-01T00:00:00",
    inputs: { left: { name: "orders", sha256: "abc" } }, conditions: { join_type: "inner" }, rules: {}, outcome: { full_rows: 13 },
  } } });

  const vm = require("vm");
  vm.createContext(sandbox);

  const scripts = fs.readFileSync(path.join(root, "static/app.js"), "utf8");
  vm.runInContext(scripts, sandbox, { filename: "app.js" });

  await settle();
  assert(el("leftDataset").children.length === 2, "数据集下拉已填充");
  assert(el("leftKeys").children.length > 0, "关联字段选择器已渲染");
  assert(el("leftNull").children.length > 0, "空值规则已渲染");

  // ---- 预览
  el("previewBtn").onclick();
  await settle();
  assert(!el("statsBlock").classList.contains("hidden"), "预览后统计区可见");
  assert(el("statResult").textContent === "13", "预计结果行数 = 13，实际 " + el("statResult").textContent);
  assert(el("statCardinality").textContent === "多对多", "基数判定");
  assert(el("statLeftUnmatched").textContent === "2", "左未匹配");
  assert(el("topKeys").querySelectorAll("tr.key-row").length === 2, "膨胀键表已渲染");
  assert(el("fieldPairs").querySelectorAll(".pair-row").length === 2, "字段配对已展示");
  assert(el("previewState").textContent === "预览已完成", "预览状态标签");
  assert(el("executeBtn").disabled === true, "未确认前禁止执行");

  // ---- 展开键明细，两侧记录都要出现
  const firstRow = el("topKeys").querySelectorAll("tr.key-row")[0];
  firstRow.querySelector("[data-expand]").onclick({ stopPropagation() {} });
  await settle();
  assert(detailCalls === 1, "展开触发 key-detail 请求");
  assert(!el("keyDetail").classList.contains("hidden"), "明细面板展开");
  const detailHtml = el("keyDetail").textContent;
  assert(detailHtml.includes("左表记录") && detailHtml.includes("右表记录"), "明细包含两侧");

  // ---- 规则对比：开启去重后再预览，对比表出现
  el("leftDedup").checked = true;
  el("leftDedup").onchange();
  assert(el("previewState").textContent === "规则已变，请重新预览", "配置变化后标记过期");
  const dedupStats = JSON.parse(JSON.stringify(stats));
  dedupStats.rows.result = 5; dedupStats.rows.matched = 5;
  dedupStats.dedup.left_dropped = 1; dedupStats.dedup.right_dropped = 1;
  dedupStats.keys.left.duplicate_keys = 1;
  responses["/api/preview"] = () => ({ body: { stats: dedupStats, columns: { left: [], right: [] }, sources: {} } });
  el("previewBtn").onclick();
  await settle();
  assert(!el("compareBlock").classList.contains("hidden"), "对比区可见");
  const compareText = el("compareTable").textContent;
  assert(compareText.includes("结果行数") && compareText.includes("去重丢弃行"), "对比指标包含行数与去重");
  assert(compareText.includes("-8"), "结果行数变化 -8");

  // ---- 确认与执行
  el("confirmCheck").checked = true;
  el("confirmCheck").onchange();
  assert(el("executeBtn").disabled === false, "确认后允许执行");
  el("executeBtn").onclick();
  await settle();
  assert(el("execState").textContent === "执行完成", "执行状态");
  assert(!el("execPreview").classList.contains("hidden"), "执行预览可见");
  const execRequests = requests.filter((r) => r.url === "/api/execute");
  assert(execRequests.length === 1 && execRequests[0].body.confirmed === true, "执行请求带确认标记");
  assert(execRequests[0].body.left.dedup_mode === "key", "执行沿用预览规则");
  assert(el("runsTable").textContent.includes("20260101-000000-abcd"), "执行记录已刷新");

  // ---- 上限保护：超限时执行请求不应带 allow_truncate（confirm 选择取消）
  sandbox.window.confirm = () => false;
  el("maxRows").value = "1";
  el("maxRows").onchange();
  el("previewBtn").onclick();
  await settle();
  el("confirmCheck").checked = true;
  el("confirmCheck").onchange();
  el("executeBtn").onclick();
  await settle();
  const limitReq = requests.filter((r) => r.url === "/api/execute").pop();
  assert(limitReq.body.allow_truncate === false, "用户取消截断时不写文件");

  console.log("frontend harness OK · 请求数", requests.length);
}

main().catch((err) => { console.error(err.stack || err); process.exit(1); });
