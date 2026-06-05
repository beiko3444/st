const state = {
  tab: "masters",
  rows: [],
  config: null,
};

const tabs = {
  masters: { title: "상품관리", meta: "마스터 상품, 채널 링크, 입고 상태를 관리합니다." },
  naver: { title: "네이버", meta: "스마트스토어 상품 재고와 판매량을 조회합니다." },
  coupang: { title: "쿠팡", meta: "쿠팡 로켓그로스 상품 재고와 판매량을 조회합니다." },
  sales: { title: "판매일보", meta: "날짜별 판매 이벤트와 요약을 확인합니다." },
  revenue: { title: "매출비교", meta: "채널별 매출과 상품별 매출 추정/집계를 봅니다." },
  keywords: { title: "키워드매출", meta: "네이버 검색 키워드 유입과 매출을 봅니다." },
  purchases: { title: "구매내역", meta: "구매내역, 주문, HTML 업로드/붙여넣기를 처리합니다." },
  cards: { title: "카드사용내역", meta: "카드 사용, 카테고리, 쿠팡 매칭, 고정비를 관리합니다." },
  fassto: { title: "파스토", meta: "파스토 상품, 재고, 입출고, 택배, 매출을 조회합니다." },
};

const $ = (selector) => document.querySelector(selector);
const tableHead = $("#dataTable thead");
const tableBody = $("#dataTable tbody");

function setStatus(message, isError = false) {
  const el = $("#statusLine");
  el.textContent = message || "";
  el.classList.toggle("error", Boolean(isError));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data.data;
}

function normalizeRows(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  if (payload.rows) return payload.rows;
  if (payload.records) return payload.records;
  if (payload.orders) return payload.orders;
  if (payload.items) return payload.items;
  if (payload.masters) return payload.masters;
  if (payload.links) return payload.links;
  if (payload.sales) return payload.sales;
  if (payload.summary && typeof payload.summary === "object") return [payload.summary];
  if (payload.snapshot?.products) return payload.snapshot.products;
  if (payload.snapshot?.rows) return payload.snapshot.rows;
  if (payload.snapshot?.summaries) return payload.snapshot.summaries;
  if (payload.data && Array.isArray(payload.data)) return payload.data;
  return [payload];
}

function compactValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function pickColumns(rows) {
  const preferred = [
    "imageUrl", "serial", "id", "name", "title", "channel", "productId", "itemId",
    "stock", "todaySales", "sales", "price", "amount", "net", "orders", "order_date",
    "order_no", "used_at", "store_name", "category", "reviewed", "syncedAt",
  ];
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row || {})))];
  const ordered = preferred.filter((key) => keys.includes(key));
  const rest = keys.filter((key) => !ordered.includes(key));
  return [...ordered, ...rest].slice(0, 14);
}

function renderTable(rows) {
  state.rows = rows;
  tableHead.innerHTML = "";
  tableBody.innerHTML = "";
  if (!rows.length) {
    tableBody.innerHTML = `<tr><td>표시할 데이터가 없습니다.</td></tr>`;
    $("#details").textContent = "{}";
    return;
  }

  const columns = pickColumns(rows);
  tableHead.innerHTML = `<tr>${columns.map((col) => `<th>${col}</th>`).join("")}</tr>`;
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.innerHTML = columns.map((col) => {
      const value = row[col];
      if (col.toLowerCase().includes("image") && value) {
        return `<td><img src="${String(value).replaceAll('"', "&quot;")}" alt=""></td>`;
      }
      return `<td title="${compactValue(value).replaceAll('"', "&quot;")}">${compactValue(value)}</td>`;
    }).join("");
    tr.addEventListener("click", () => {
      $("#details").textContent = JSON.stringify(row, null, 2);
    });
    tableBody.appendChild(tr);
  }
  $("#details").textContent = JSON.stringify(rows[0], null, 2);
}

function button(label, handler, className = "") {
  const btn = document.createElement("button");
  btn.textContent = label;
  if (className) btn.className = className;
  btn.addEventListener("click", () => {
    Promise.resolve(handler()).catch((error) => setStatus(error.message, true));
  });
  return btn;
}

function input(name, value = "", type = "text") {
  const el = document.createElement("input");
  el.name = name;
  el.type = type;
  el.value = value;
  return el;
}

function select(name, options) {
  const el = document.createElement("select");
  el.name = name;
  for (const [value, label] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    el.appendChild(option);
  }
  return el;
}

function toolbar(...children) {
  const tb = $("#toolbar");
  tb.innerHTML = "";
  children.forEach((child) => tb.appendChild(child));
}

function showModal(title, bodyNode) {
  $("#modalTitle").textContent = title;
  const body = $("#modalBody");
  body.innerHTML = "";
  body.appendChild(bodyNode);
  $("#modal").showModal();
}

async function loadJobs(job) {
  setStatus(`작업 시작: ${job.id}`);
  const timer = setInterval(async () => {
    try {
      const current = await api(`/api/jobs/${job.id}`);
      setStatus(`${current.name}: ${current.status} ${current.progress}%`);
      if (["succeeded", "failed"].includes(current.status)) {
        clearInterval(timer);
        if (current.status === "failed") setStatus(current.error || "작업 실패", true);
      }
    } catch (error) {
      clearInterval(timer);
      setStatus(error.message, true);
    }
  }, 1200);
}

async function loadCurrentTab() {
  const info = tabs[state.tab];
  $("#viewTitle").textContent = info.title;
  $("#viewMeta").textContent = info.meta;
  document.querySelectorAll(".tab-button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === state.tab);
  });

  try {
    setStatus("Loading...");
    if (state.tab === "masters") return await loadMasters();
    if (state.tab === "naver" || state.tab === "coupang") return await loadChannel(state.tab);
    if (state.tab === "sales") return await loadSales();
    if (state.tab === "revenue") return await loadRevenue();
    if (state.tab === "keywords") return await loadKeywords();
    if (state.tab === "purchases") return await loadPurchases();
    if (state.tab === "cards") return await loadCards();
    if (state.tab === "fassto") return await loadFassto();
  } catch (error) {
    setStatus(error.message, true);
    renderTable([]);
  }
}

async function loadMasters() {
  toolbar(
    button("New Master", () => {
      const form = document.createElement("div");
      const name = input("name");
      name.placeholder = "마스터 이름";
      const cost = input("unit_cost", "", "number");
      cost.placeholder = "원가";
      form.append(name, cost, button("Save", async () => {
        await api("/api/masters", {
          method: "POST",
          body: JSON.stringify({ name: name.value, unit_cost: cost.value || null }),
        });
        $("#modal").close();
        loadCurrentTab();
      }, "primary-button"));
      showModal("새 마스터", form);
    }),
  );
  const data = await api("/api/masters?include_links=1");
  renderTable(normalizeRows(data.masters ? data : data.data));
  setStatus(`${normalizeRows(data.masters ? data : data.data).length} rows`);
}

async function loadChannel(channel) {
  const q = input("q");
  q.placeholder = "상품명 검색";
  toolbar(
    q,
    button("Search", () => loadChannel(channel)),
    button("Live Sync", async () => loadJobs(await api(`/api/channels/${channel}/sync`, { method: "POST" })), "primary-button"),
  );
  const data = await api(`/api/channels/${channel}?q=${encodeURIComponent(q.value)}`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`${rows.length} rows${data.warnings?.length ? `, ${data.warnings.join(", ")}` : ""}`);
}

async function loadSales() {
  const dateInput = input("date", new Date().toISOString().slice(0, 10), "date");
  toolbar(
    dateInput,
    button("Load", () => loadSalesWithDate(dateInput.value)),
    button("Dates", async () => renderTable(normalizeRows(await api("/api/sales/dates")))),
  );
  await loadSalesWithDate(dateInput.value);
}

async function loadSalesWithDate(value) {
  const data = await api(`/api/sales?date=${encodeURIComponent(value)}`);
  const rows = normalizeRows(data.sales ? data : data.data);
  renderTable(rows);
  setStatus(`${rows.length} sales rows`);
}

async function loadRevenue() {
  const days = select("period_days", [["30", "30 days"], ["7", "7 days"], ["90", "90 days"]]);
  toolbar(days, button("Load", () => loadRevenue()), button("Sync", async () => loadJobs(await api(`/api/revenue/sync?period_days=${days.value}`, { method: "POST" })), "primary-button"));
  const data = await api(`/api/revenue?period_days=${days.value}`);
  const rows = normalizeRows(data.snapshot?.products?.length ? { rows: data.snapshot.products } : data.snapshot?.summaries);
  renderTable(rows);
  setStatus(`${rows.length} revenue rows`);
}

async function loadKeywords() {
  const days = select("period_days", [["30", "30 days"], ["7", "7 days"], ["90", "90 days"]]);
  toolbar(days, button("Load", () => loadKeywords()), button("Sync", async () => loadJobs(await api(`/api/keywords/sync?period_days=${days.value}`, { method: "POST" })), "primary-button"));
  const data = await api(`/api/keywords?period_days=${days.value}`);
  const rows = normalizeRows(data.snapshot);
  renderTable(rows);
  setStatus(`${rows.length} keyword rows`);
}

async function loadPurchases() {
  const channel = select("channel", [["all", "전체"], ["naver", "네이버"], ["coupang", "쿠팡"]]);
  toolbar(
    channel,
    button("Records", () => loadPurchaseRows("records", channel.value)),
    button("Orders", () => loadPurchaseRows("orders", channel.value)),
    button("Paste HTML", () => showPasteImport(channel.value)),
  );
  await loadPurchaseRows("records", channel.value);
}

async function loadPurchaseRows(kind, channel) {
  const data = await api(`/api/purchases/${kind}?channel=${channel}&limit=2000`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`${rows.length} ${kind}`);
}

function showPasteImport(channel) {
  const form = document.createElement("div");
  const text = document.createElement("textarea");
  text.placeholder = "주문내역 HTML 또는 텍스트를 붙여넣으세요.";
  form.append(text, button("Import", async () => {
    const result = await api("/api/purchases/import/text", {
      method: "POST",
      body: JSON.stringify({ channel, text: text.value }),
    });
    $("#modal").close();
    setStatus(`parsed ${result.parsed}, saved ${result.saved}`);
    loadCurrentTab();
  }, "primary-button"));
  showModal("구매내역 가져오기", form);
}

async function loadCards() {
  const start = input("start_date", new Date(new Date().setDate(new Date().getDate() - 30)).toISOString().slice(0, 10), "date");
  const end = input("end_date", new Date().toISOString().slice(0, 10), "date");
  toolbar(
    start,
    end,
    button("Load", () => loadCardRows(start.value, end.value)),
    button("Sync", async () => loadJobs(await api("/api/cards/sync", {
      method: "POST",
      body: JSON.stringify({ start_date: start.value, end_date: end.value }),
    })), "primary-button"),
    button("Match Coupang", async () => loadJobs(await api("/api/cards/coupang-match", {
      method: "POST",
      body: JSON.stringify({ start_date: start.value, end_date: end.value }),
    }))),
  );
  await loadCardRows(start.value, end.value);
}

async function loadCardRows(start, end) {
  const data = await api(`/api/cards/usages?start_date=${start}&end_date=${end}&limit=5000`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`${rows.length} card rows`);
}

async function loadFassto() {
  const section = select("section", [
    ["goods", "상품"],
    ["stock", "재고"],
    ["warehousing", "입고"],
    ["delivery", "출고"],
    ["parcels", "택배"],
    ["revenue", "매출"],
  ]);
  const start = input("start", yyyymmdd(-30));
  const end = input("end", yyyymmdd(0));
  toolbar(
    section,
    start,
    end,
    button("Load", () => loadFasstoSection(section.value, start.value, end.value)),
    button("CSV", () => {
      window.location.href = `/api/fassto/${section.value}?start=${start.value}&end=${end.value}&download=true`;
    }),
  );
  await loadFasstoSection(section.value, start.value, end.value);
}

function yyyymmdd(deltaDays) {
  const d = new Date();
  d.setDate(d.getDate() + deltaDays);
  return d.toISOString().slice(0, 10).replaceAll("-", "");
}

async function loadFasstoSection(section, start, end) {
  const data = await api(`/api/fassto/${section}?start=${start}&end=${end}`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`${rows.length} fassto rows`);
}

document.querySelectorAll(".tab-button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tab = btn.dataset.tab;
    loadCurrentTab();
  });
});

$("#refreshBtn").addEventListener("click", loadCurrentTab);
$("#syncBtn").addEventListener("click", async () => {
  if (state.tab === "naver" || state.tab === "coupang") {
    loadJobs(await api(`/api/channels/${state.tab}/sync`, { method: "POST" }));
  } else if (state.tab === "revenue") {
    loadJobs(await api("/api/revenue/sync", { method: "POST" }));
  } else if (state.tab === "keywords") {
    loadJobs(await api("/api/keywords/sync", { method: "POST" }));
  } else {
    setStatus("이 탭은 화면별 Sync 버튼을 사용하세요.");
  }
});

api("/api/config/status")
  .then((config) => {
    state.config = config;
    $("#runtimeLabel").textContent = config.vercel ? "Vercel" : "Local/Web";
  })
  .catch(() => {});

loadCurrentTab();
