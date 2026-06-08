const state = {
  tab: "masters",
  rows: [],
  config: null,
  fasstoSection: "goods",
  selectedIndex: 0,
  channelFavoriteFilter: { naver: "all", coupang: "all" },
  currentPurchaseKind: "records",
  currentPurchaseChannel: "all",
  activeJob: null,
  cardCategories: null,
};

const tabs = {
  masters: { title: "상품관리", meta: "마스터 상품" },
  naver: { title: "네이버", meta: "스마트스토어 상품" },
  coupang: { title: "쿠팡", meta: "쿠팡 로켓그로스 상품" },
  sales: { title: "판매일보", meta: "날짜별 판매 현황" },
  revenue: { title: "매출비교", meta: "채널별 매출" },
  keywords: { title: "키워드매출", meta: "네이버 키워드 매출" },
  purchases: { title: "구매내역", meta: "구매/주문 내역" },
  cards: { title: "카드사용내역", meta: "카드 사용 내역" },
  fassto: { title: "파스토", meta: "파스토 물류" },
};

const $ = (selector) => document.querySelector(selector);
const tableHead = $("#dataTable thead");
const tableBody = $("#dataTable tbody");

function setStatus(message, isError = false) {
  const el = $("#statusLine");
  el.textContent = message || "";
  el.classList.toggle("error", Boolean(isError));
}

function setProgress(percent = 0, active = false) {
  const bar = $("#syncProgress");
  if (!bar) return;
  bar.classList.toggle("active", active);
  bar.style.setProperty("--progress", `${Math.max(0, Math.min(100, Number(percent) || 0))}%`);
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
  if (payload.snapshot?.products) return payload.snapshot.products;
  if (payload.snapshot?.rows) return payload.snapshot.rows;
  if (payload.snapshot?.summaries) return payload.snapshot.summaries;
  if (payload.data && Array.isArray(payload.data)) return payload.data;
  if (payload.summary && typeof payload.summary === "object") return [payload.summary];
  return [payload];
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function valueFrom(row, keys) {
  for (const key of keys) {
    if (row && row[key] !== undefined && row[key] !== null) return row[key];
  }
  return "";
}

function compactValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatNumber(value) {
  if (value === "" || value === null || value === undefined || Number.isNaN(Number(value))) return "";
  return Number(value).toLocaleString("ko-KR");
}

function formatPrice(value) {
  const formatted = formatNumber(value);
  return formatted ? `${formatted}원` : "";
}

function formatDate(value) {
  const text = compactValue(value);
  if (!text) return "";
  return text.replace("T", " ").slice(0, 16);
}

function selectedRow() {
  return state.rows[state.selectedIndex] || null;
}

function productIdentity(row) {
  return compactValue(valueFrom(row, ["identityKey", "productIdentityKey", "productKey"]));
}

function productUrl(row) {
  return compactValue(valueFrom(row, ["productUrl", "product_url", "naverUrl", "coupangUrl", "source_url", "sourceUrl"]));
}

function channelLabel(channel) {
  return channel === "naver" ? "네이버" : channel === "coupang" ? "쿠팡" : channel;
}

function parseInteger(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(String(value).replaceAll(",", ""));
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

function todayIso(deltaDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function channelSoldOutDays(row, days) {
  const stock = Number(valueFrom(row, ["stock"]));
  const sales = Number(valueFrom(row, ["sales"]));
  if (!stock || !sales || sales <= 0) return "";
  return `${Math.ceil(stock / (sales / days)).toLocaleString("ko-KR")}일`;
}

function channelMonthlyRevenue(row, days) {
  const sales = Number(valueFrom(row, ["sales"]));
  const price = Number(valueFrom(row, ["price"]));
  if (!sales || !price) return "";
  return Math.round((sales / days) * 30 * price);
}

function sumGoods(row, keys) {
  const goods = row?.goods;
  if (Array.isArray(goods)) {
    return goods.reduce((total, item) => {
      const raw = valueFrom(item || {}, keys);
      const qty = Number(raw);
      return total + (Number.isFinite(qty) ? qty : 0);
    }, 0);
  }
  const direct = Number(valueFrom(row, keys));
  return Number.isFinite(direct) ? direct : "";
}

function col(label, keys, width, options = {}) {
  const normalizedKeys = Array.isArray(keys) ? keys : [keys];
  return { label, keys: normalizedKeys, width, ...options };
}

function imageCol(label, keys, width = 68) {
  return col(label, keys, width, { type: "image", className: "image center" });
}

function numberCol(label, keys, width = 88, options = {}) {
  return col(label, keys, width, { className: "number", format: formatNumber, ...options });
}

function priceCol(label, keys, width = 108, options = {}) {
  return numberCol(label, keys, width, { format: formatPrice, ...options });
}

function centerCol(label, keys, width = 80, options = {}) {
  return col(label, keys, width, { className: "center", ...options });
}

const channelColumns = (days) => [
  centerCol("★", "__favorite", 36, { derive: (row) => row?.isFavorite ? "★" : "☆" }),
  centerCol("연번", ["serial"], 44),
  imageCol("상품이미지", ["imageUrl", "image_url"], 64),
  col("상품명", ["name", "title"], 360),
  col("마스터", ["linkedMasterName"], 150),
  numberCol("배수", ["linkMultiplier"], 52),
  numberCol("재고", ["stock"], 68),
  numberCol("오늘판매", ["todaySales", "today_sales"], 76),
  numberCol(`${days}일`, ["sales"], 66),
  centerCol("품절예상", "__soldOut", 84, { derive: (row) => channelSoldOutDays(row, days) }),
  priceCol("예상월매출", "__monthly", 106, { derive: (row) => channelMonthlyRevenue(row, days) }),
  priceCol("판매가", ["price"], 86),
];

const schemas = {
  masters: [
    imageCol("이미지", ["imageUrl", "image_url"], 56),
    col("이름", ["name"], 260),
    priceCol("원가", ["unitCost", "unit_cost"], 80),
    priceCol("네이버가", ["naverPrice"], 92),
    priceCol("쿠팡가", ["coupangPrice"], 88),
    numberCol("네이버재고", ["naverStock"], 86),
    numberCol("네이버입고", ["naverInboundPending"], 86),
    numberCol("쿠팡재고", ["coupangStock"], 86),
    numberCol("쿠팡입고", ["coupangInboundPending"], 86),
    numberCol("총재고", ["totalStock"], 80),
    numberCol("총입고", ["totalInboundPending"], 72),
    priceCol("재고원가", ["stockCost"], 110),
    numberCol("네이버(오늘)", ["naverTodaySales"], 92),
    numberCol("쿠팡(오늘)", ["coupangTodaySales"], 84),
    numberCol("오늘판매", ["totalTodaySales"], 86),
    priceCol("오늘매출", ["todayRevenue"], 110),
    numberCol("네이버판매(30일)", ["naverSales"], 116),
    numberCol("쿠팡판매(30일)", ["coupangSales"], 110),
    numberCol("총판매(30일)", ["totalSales"], 108),
    centerCol("연결", ["linkCount"], 56),
  ],
  sales: [
    centerCol("채널", ["channel"], 80),
    col("상품ID", ["product_id", "productId"], 120),
    col("상품명", ["name", "title", "product_name"], 360),
    numberCol("수량", ["quantity", "qty", "today_sales", "todaySales"], 82),
    priceCol("매출", ["amount", "revenue", "sales_amount"], 120),
    col("기록시각", ["recorded_at", "created_at", "syncedAt"], 150, { format: formatDate }),
  ],
  revenue: [
    centerCol("채널", ["channel"], 80),
    col("상품ID", ["product_id", "productId", "id"], 120),
    imageCol("상품이미지", ["imageUrl", "image_url"], 78),
    col("상품명", ["name", "title", "product_name"], 430),
    numberCol("주문수", ["orders", "order_count", "count"], 100),
    priceCol("총매출", ["total", "gross", "revenue", "amount"], 140),
    priceCol("환불", ["refund", "refunds"], 130),
    priceCol("순매출", ["net", "net_revenue"], 140),
    centerCol("데이터유형", ["data_type", "source", "type"], 110),
  ],
  keywords: [
    centerCol("연번", ["serial", "rank"], 54),
    col("키워드", ["keyword", "query"], 300),
    priceCol("매출", ["revenue", "amount", "sales"], 140),
    numberCol("주문수", ["orders", "order_count"], 100),
    numberCol("유입수", ["visits", "clicks", "traffic"], 100),
    centerCol("전환율", ["conversion_rate", "conversionRate"], 100),
    priceCol("객단가", ["average_order_value", "aov"], 110),
    centerCol("데이터출처", ["source", "data_type"], 110),
  ],
  purchases: [
    centerCol("채널", ["channel"], 76),
    col("일자", ["order_date", "orderDate"], 104),
    col("주문번호", ["order_no", "orderNo"], 150),
    col("상품/내역", ["title", "name", "raw_text"], 420),
    priceCol("금액", ["amount", "payment_total", "card_amount"], 118),
    col("결제수단", ["payment_method", "paymentMethod"], 120),
    col("계정", ["account_label", "accountLabel"], 120),
    col("가져온시각", ["imported_at", "importedAt"], 150, { format: formatDate }),
  ],
  cards: [
    col("날짜", ["used_at", "usedAt"], 130, { format: formatDate }),
    col("카테고리", ["category"], 110),
    col("가맹점", ["store_name", "storeName"], 260),
    priceCol("금액", ["amount"], 120),
    col("카드", ["card_num", "cardNum"], 150),
    centerCol("검토", ["reviewed"], 66),
    col("쿠팡매칭", ["coupang_purchase_id", "coupangPurchaseId"], 150),
    col("메모", ["memo"], 260),
  ],
};

const fasstoSchemas = {
  goods: [
    col("상품코드", ["cstGodCd", "godCd", "goodsCd", "itemCd"], 120),
    col("상품명", ["godNm", "goodsNm", "itemNm", "godName"], 300),
    col("바코드", ["godBarcd", "barcode", "barCd"], 130),
    centerCol("사용", ["useYn", "use_yn"], 64),
    centerCol("상품구분", ["godType", "goodsType"], 84),
    col("카테고리", ["category", "cateNm", "categoryName"], 130),
    col("공급사", ["supNm", "supplierName"], 130),
    priceCol("매입가", ["inPr", "purchasePrice"], 90),
    priceCol("판매가", ["salPr", "salePrice"], 90),
    numberCol("중량(g)", ["weight", "weightGram"], 84),
    numberCol("박스입수", ["boxQty", "boxUnitQty"], 80),
    numberCol("안전재고", ["safetyStock"], 84),
    col("최초입고일", ["firstInDt", "firstInDate"], 110),
  ],
  stock: [
    centerCol("상태", ["status", "alert"], 72),
    col("상품코드", ["cstGodCd", "godCd", "goodsCd", "itemCd"], 120),
    col("상품명", ["godNm", "goodsNm", "itemNm", "godName"], 280),
    col("바코드", ["godBarcd", "barcode", "barCd"], 130),
    col("창고", ["whNm", "whCd", "warehouseName"], 110),
    numberCol("총재고", ["stockQty", "stockQnt", "stock"], 90),
    numberCol("가용재고", ["canStockQty", "availableStock"], 90),
    numberCol("불량재고", ["badStockQty", "badStock"], 90),
    numberCol("안전재고", ["safetyStock"], 86),
    col("유통기한", ["distTermDt", "expirationDate"], 100),
    col("공급사", ["supNm", "supplierName"], 120),
    col("전표번호", ["slipNo", "slip_no"], 120),
    col("시리얼", ["goodsSerialNo", "goodsSerno"], 160),
  ],
  warehousing: [
    col("전표번호", ["slipNo", "slip_no"], 120),
    col("입고예정일", ["ordDt", "inPlanDt", "inExpectDt"], 110),
    col("창고", ["whNm", "whCd"], 110),
    col("작업상태", ["wrkStatNm", "wrkStat", "status"], 110),
    col("공급사", ["supNm", "supCd"], 130),
    numberCol("SKU", "__sku", 64, { derive: (row) => Array.isArray(row?.goods) ? row.goods.length : "" }),
    numberCol("요청수량", "__ordQty", 86, { derive: (row) => sumGoods(row, ["ordQty", "reqQty"]) }),
    numberCol("입고수량", "__inQty", 86, { derive: (row) => sumGoods(row, ["inQty"]) }),
    numberCol("검수수량", "__inspQty", 86, { derive: (row) => sumGoods(row, ["inspQty", "inspectQty"]) }),
    col("입고경로", ["inWay", "inWayNm"], 110),
    col("택배사", ["parcelComp", "parcelNm"], 110),
    col("송장번호", ["parcelInvoiceNo", "invoiceNo"], 140),
  ],
  delivery: [
    col("전표번호", ["slipNo", "slip_no"], 120),
    col("출고일", ["outDt", "deliveryDt"], 110),
    col("주문일", ["ordDt", "orderDt"], 110),
    col("판매채널", ["salesChannel", "mallNm", "channel"], 120),
    col("작업상태", ["wrkStatNm", "wrkStat", "status"], 110),
    col("출고구분", ["outDiv", "outDivNm"], 86),
    numberCol("주문수량", "__ordQty", 86, { derive: (row) => sumGoods(row, ["ordQty", "outQty"]) }),
    col("수취인", ["rcvrNm", "receiverName", "receiver"], 110),
    col("연락처", ["rcvrTel", "receiverTel", "phone"], 120),
    col("송장번호", ["invoiceNo", "parcelInvoiceNo"], 140),
    col("택배사", ["parcelNm", "parcelCd"], 110),
    col("창고", ["whNm", "whCd"], 110),
    col("수정시각", ["updatedAt", "modDt"], 140, { format: formatDate }),
  ],
  parcels: [
    col("전표번호", ["slipNo", "slip_no"], 120),
    col("포장일", ["packDt", "packingDt"], 110),
    col("배송상태", ["deliveryStatus", "dlvStatNm", "status"], 110),
    col("박스구분", ["boxDiv", "boxType"], 90),
    col("박스명", ["boxNm", "boxName"], 120),
    col("송장번호", ["invoiceNo", "parcelInvoiceNo"], 140),
    col("택배사", ["parcelNm", "parcelCd"], 110),
    col("상품명", ["godNm", "goodsNm", "itemNm"], 260),
    numberCol("포장수량", ["packQty", "packingQty", "outQty"], 86),
    col("SKU", ["cstGodCd", "godCd", "goodsCd"], 110),
    col("수취인", ["rcvrNm", "receiverName"], 110),
    col("판매처", ["mallNm", "seller"], 120),
    col("판매채널", ["salesChannel", "channel"], 120),
    col("지연", ["delayNm", "delayYn"], 70),
    col("배송누락", ["dlvMisYn"], 80),
    col("반품예정일", ["returnPlanDt"], 110),
    col("주소", ["addr", "address"], 260),
  ],
  revenue: [
    col("출고일", ["outDt", "deliveryDt"], 110),
    col("전표번호", ["slipNo", "slip_no"], 120),
    col("판매채널", ["salesChannel", "mallNm", "channel"], 120),
    col("주문번호", ["ordNo", "orderNo"], 150),
    col("상품주문번호", ["ordDtlNo", "productOrderNo"], 150),
    col("수취인", ["rcvrNm", "receiverName"], 110),
    col("상품코드", ["cstGodCd", "godCd", "goodsCd"], 120),
    col("상품명", ["godNm", "goodsNm", "itemNm"], 300),
    centerCol("상품구분", ["godType", "goodsType"], 84),
    numberCol("출고수량", ["outQty", "qty"], 86),
    priceCol("정상가", ["normalPr", "normalPrice"], 100),
    priceCol("판매가", ["salPr", "salePrice"], 100),
    priceCol("할인액", ["dcAmt", "discountAmount"], 100),
    priceCol("판매자할인", ["sellerDcAmt"], 110),
    priceCol("네이버할인", ["naverDcAmt"], 110),
    priceCol("소계(판매)", ["saleAmt", "amount"], 110),
  ],
};

const genericLabels = {
  imageUrl: "상품이미지",
  image_url: "상품이미지",
  serial: "연번",
  id: "ID",
  name: "상품명",
  title: "상품/내역",
  channel: "채널",
  productId: "상품ID",
  product_id: "상품ID",
  itemId: "옵션ID",
  item_id: "옵션ID",
  stock: "재고",
  todaySales: "오늘판매",
  today_sales: "오늘판매",
  sales: "판매량",
  price: "판매가",
  amount: "금액",
  net: "순매출",
  orders: "주문수",
  order_date: "일자",
  order_no: "주문번호",
  used_at: "날짜",
  store_name: "가맹점",
  category: "카테고리",
  reviewed: "검토",
  syncedAt: "동기화시각",
  memo: "메모",
};

function getColumns(rows) {
  if (state.tab === "naver") return channelColumns(30);
  if (state.tab === "coupang") return channelColumns(30);
  if (state.tab === "fassto") return fasstoSchemas[state.fasstoSection] || fasstoSchemas.goods;
  if (schemas[state.tab]) return schemas[state.tab];

  const preferred = [
    "imageUrl", "serial", "id", "name", "title", "channel", "productId", "itemId",
    "stock", "todaySales", "sales", "price", "amount", "net", "orders", "order_date",
    "order_no", "used_at", "store_name", "category", "reviewed", "syncedAt",
  ];
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row || {})))];
  const ordered = preferred.filter((key) => keys.includes(key));
  const rest = keys.filter((key) => !ordered.includes(key));
  return [...ordered, ...rest].slice(0, 14).map((key) => col(genericLabels[key] || key, key, 120));
}

function renderCell(row, column) {
  const rawValue = typeof column.derive === "function" ? column.derive(row) : valueFrom(row, column.keys);
  const value = typeof column.format === "function" ? column.format(rawValue, row) : rawValue;
  const text = compactValue(value);
  const title = escapeHtml(compactValue(rawValue));
  const classes = [column.className || ""];
  if (column.keys?.includes("__favorite")) classes.push("favorite-cell");
  if (column.type === "image") {
    const url = compactValue(rawValue);
    const href = productUrl(row);
    const img = url ? `<img src="${escapeHtml(url)}" alt="">` : "";
    const linked = img && href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${img}</a>` : img;
    return `<td class="${classes.join(" ")}" style="--col-width:${column.width}px">${linked}</td>`;
  }
  return `<td class="${classes.join(" ")}" style="--col-width:${column.width}px" title="${title}">${escapeHtml(text)}</td>`;
}

function renderTable(rows) {
  state.rows = rows;
  state.selectedIndex = 0;
  tableHead.innerHTML = "";
  tableBody.innerHTML = "";

  const columns = getColumns(rows);
  tableHead.innerHTML = `<tr>${columns.map((column) => {
    const classes = column.className || "";
    return `<th class="${classes}" style="--col-width:${column.width}px">${escapeHtml(column.label)}</th>`;
  }).join("")}</tr>`;

  if (!rows.length) {
    tableBody.innerHTML = `<tr><td class="empty-cell" colspan="${Math.max(columns.length, 1)}">표시할 데이터가 없습니다.</td></tr>`;
    $("#details").textContent = "{}";
    return;
  }

  rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.innerHTML = columns.map((column) => renderCell(row, column)).join("");
    tr.addEventListener("click", () => selectRow(index));
    tr.addEventListener("dblclick", () => openSelectedAction());
    tableBody.appendChild(tr);
  });
  selectRow(0);
}

function selectRow(index) {
  state.selectedIndex = index;
  document.querySelectorAll("#dataTable tbody tr").forEach((tr, rowIndex) => {
    tr.classList.toggle("selected", rowIndex === index);
  });
  const row = state.rows[index] || {};
  $("#details").textContent = JSON.stringify(row, null, 2);
  renderDetailPanel(row);
}

function renderDetailPanel(row) {
  const meta = $("#detailMeta");
  const body = $("#detailTable tbody");
  if (!meta || !body) return;

  const name = compactValue(valueFrom(row, ["name", "title", "product_name"]));
  meta.textContent = name || "선택 상품 판매 로그";

  const qty = Number(valueFrom(row, ["todaySales", "today_sales", "quantity", "qty"])) || 0;
  const price = Number(valueFrom(row, ["price"])) || 0;
  const amount = price > 0 ? qty * price : Number(valueFrom(row, ["amount", "revenue", "sales_amount"])) || 0;
  const time = formatDate(valueFrom(row, ["syncedAt", "recorded_at", "created_at"])) || "오늘";
  if (!qty && !amount) {
    body.innerHTML = `<tr><td colspan="3" class="empty-cell">표시할 데이터가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = `
    <tr>
      <td style="--col-width:80px">${escapeHtml(time)}</td>
      <td class="number" style="--col-width:56px">${escapeHtml(formatNumber(qty))}</td>
      <td class="number" style="--col-width:120px">${escapeHtml(formatPrice(amount))}</td>
    </tr>
  `;
}

function openSelectedAction() {
  if (state.tab === "masters") return showMasterEditor(selectedRow());
  if (state.tab === "naver" || state.tab === "coupang") return showChannelProductDetail(state.tab, selectedRow());
  if (state.tab === "purchases") return showPurchaseDetail(selectedRow());
  if (state.tab === "cards") return showCardUsageEditor(selectedRow());
  if (state.tab === "fassto") return showFasstoDetail();
  return showSelectedDetail();
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

function label(text) {
  const el = document.createElement("span");
  el.className = "toolbar-label";
  el.textContent = text;
  return el;
}

function spacer() {
  const el = document.createElement("span");
  el.className = "spacer";
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

function closeModal() {
  const modal = $("#modal");
  if (modal?.open) modal.close();
}

function field(labelText, control) {
  const wrap = document.createElement("label");
  wrap.className = "form-field";
  const span = document.createElement("span");
  span.textContent = labelText;
  wrap.append(span, control);
  return wrap;
}

function actions(...buttons) {
  const wrap = document.createElement("div");
  wrap.className = "modal-actions";
  buttons.forEach((btn) => wrap.appendChild(btn));
  return wrap;
}

function preBlock(value) {
  const pre = document.createElement("pre");
  pre.textContent = typeof value === "string" ? value : JSON.stringify(value || {}, null, 2);
  return pre;
}

function selectedRequired(message = "선택된 행이 없습니다.") {
  const row = selectedRow();
  if (!row) {
    setStatus(message, true);
    return null;
  }
  return row;
}

function showSelectedDetail() {
  const row = state.rows[state.selectedIndex];
  const body = document.createElement("pre");
  body.textContent = JSON.stringify(row || {}, null, 2);
  showModal("상세", body);
}

async function loadJobs(job) {
  state.activeJob = job;
  setProgress(12, true);
  setStatus(`작업 시작: ${job.id}`);
  const timer = setInterval(async () => {
    try {
      const current = await api(`/api/jobs/${job.id}`);
      state.activeJob = current;
      const logEl = document.querySelector("#jobLog");
      if (logEl) {
        logEl.textContent = [
          `${current.name}: ${current.status} ${current.progress}%`,
          ...(current.logs || []),
          current.error ? `ERROR: ${current.error}` : "",
          current.result ? JSON.stringify(current.result, null, 2) : "",
        ].filter(Boolean).join("\n");
      }
      setProgress(current.progress || 0, true);
      setStatus(`${current.name}: ${current.status} ${current.progress}%`);
      if (["succeeded", "failed"].includes(current.status)) {
        clearInterval(timer);
        setProgress(current.status === "succeeded" ? 100 : current.progress || 0, current.status !== "succeeded");
        if (current.status === "failed") {
          setStatus(current.error || "작업 실패", true);
        } else {
          setTimeout(() => setProgress(0, false), 600);
          loadCurrentTab();
        }
      }
    } catch (error) {
      clearInterval(timer);
      setProgress(0, false);
      setStatus(error.message, true);
    }
  }, 1200);
}

function showJobLog(job) {
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const pre = document.createElement("pre");
  pre.id = "jobLog";
  pre.textContent = `${job.name || "job"}: ${job.status || "queued"}`;
  wrap.append(pre);
  showModal("작업 로그", wrap);
}

async function startLoggedJob(jobPromise) {
  const job = await jobPromise;
  showJobLog(job);
  await loadJobs(job);
}

async function loadCurrentTab() {
  const info = tabs[state.tab];
  document.body.dataset.tab = state.tab;
  $("#viewTitle").textContent = info.title;
  $("#viewMeta").textContent = info.meta;
  document.querySelectorAll(".tab-button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === state.tab);
  });

  try {
    setStatus("조회 중...");
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
    button("새 마스터", () => showMasterEditor(null)),
    button("수정", () => showMasterEditor(selectedRequired())),
    button("입고관리", () => showInboundManager(selectedRequired())),
    button("상품URL", () => openMasterUrls(selectedRequired())),
    button("새로고침", loadMasters),
    spacer(),
  );
  const data = await api("/api/masters?include_links=1");
  const rows = normalizeRows(data.masters ? data : data.data);
  renderTable(rows);
  const unlinked = data.unlinked ? ` | 미연결 네이버 ${data.unlinked.naver || 0}, 쿠팡 ${data.unlinked.coupang || 0}` : "";
  setStatus(`마스터 ${rows.length.toLocaleString("ko-KR")}개${unlinked}`);
}

function openMasterUrls(row) {
  if (!row) return;
  [row.naverUrl, row.coupangUrl].filter(Boolean).forEach((url) => window.open(url, "_blank", "noreferrer"));
  if (!row.naverUrl && !row.coupangUrl) setStatus("연결된 상품 URL이 없습니다.", true);
}

async function showMasterEditor(row) {
  const isNew = !row?.id;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const name = input("name", row?.name || "");
  const cost = input("unitCost", row?.unitCost ?? "", "number");
  const memo = document.createElement("textarea");
  memo.value = row?.memo || "";
  memo.placeholder = "메모";
  memo.style.minHeight = "90px";
  wrap.append(field("마스터명", name), field("원가", cost), field("메모", memo));

  if (!isNew && Array.isArray(row.linked) && row.linked.length) {
    const list = document.createElement("div");
    list.className = "linked-list";
    row.linked.forEach((link) => {
      const item = document.createElement("div");
      item.className = "linked-item";
      item.innerHTML = `<strong>${escapeHtml(channelLabel(link.channel))}</strong> ${escapeHtml(link.name || link.productKey || "")} <span>배수 ${escapeHtml(link.multiplier || 1)}</span>`;
      item.append(
        button("대표", async () => {
          await api(`/api/masters/${row.id}/representative`, {
            method: "PUT",
            body: JSON.stringify({ channel: link.channel, product_key: link.productKey }),
          });
          closeModal();
          loadCurrentTab();
        }),
        button("배수", async () => {
          const next = prompt("배수", link.multiplier || 1);
          if (!next) return;
          await api("/api/master-links/multiplier", {
            method: "PUT",
            body: JSON.stringify({ channel: link.channel, product_key: link.productKey, multiplier: parseInteger(next) || 1 }),
          });
          closeModal();
          loadCurrentTab();
        }),
        button("해제", async () => {
          await api(`/api/master-links?channel=${encodeURIComponent(link.channel)}&product_key=${encodeURIComponent(link.productKey)}`, { method: "DELETE" });
          closeModal();
          loadCurrentTab();
        }, "danger-button"),
      );
      list.appendChild(item);
    });
    wrap.append(list);
  }

  const save = button("저장", async () => {
    const payload = {
      name: name.value.trim(),
      unit_cost: parseInteger(cost.value),
      memo: memo.value.trim() || null,
      clear_unit_cost: cost.value === "",
      clear_memo: memo.value.trim() === "",
    };
    if (isNew) {
      await api("/api/masters", { method: "POST", body: JSON.stringify(payload) });
    } else {
      await api(`/api/masters/${row.id}`, { method: "PATCH", body: JSON.stringify(payload) });
    }
    closeModal();
    loadCurrentTab();
  }, "primary-button");
  const deleteBtn = button("삭제", async () => {
    if (!row?.id || !confirm("마스터를 삭제할까요? 연결도 같이 해제됩니다.")) return;
    await api(`/api/masters/${row.id}`, { method: "DELETE" });
    closeModal();
    loadCurrentTab();
  }, "danger-button");
  wrap.append(isNew ? actions(save) : actions(save, deleteBtn));
  showModal(isNew ? "새 마스터" : "마스터 상세", wrap);
}

async function showInboundManager(row) {
  if (!row?.id) return;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const receipt = input("receiptDate", todayIso(), "date");
  const channel = select("channel", [["naver", "네이버"], ["coupang", "쿠팡"]]);
  const qty = input("quantity", "", "number");
  qty.placeholder = "수량";
  const list = document.createElement("div");
  list.className = "linked-list";
  async function refresh() {
    list.textContent = "조회 중...";
    const data = await api(`/api/stock-inbounds?master_id=${encodeURIComponent(row.id)}`);
    const items = normalizeRows(data.items ? data.items : data);
    if (!items.length) {
      list.innerHTML = `<div class="empty-cell">입고 예정 내역이 없습니다.</div>`;
      return;
    }
    list.innerHTML = "";
    items.forEach((item) => {
      const line = document.createElement("div");
      line.className = "linked-item";
      line.innerHTML = `${escapeHtml(item.receipt_date || item.receiptDate || "")} <strong>${escapeHtml(channelLabel(item.channel))}</strong> ${escapeHtml(formatNumber(item.remaining_qty ?? item.remainingQty ?? item.input_qty ?? item.inputQty))}`;
      line.append(button("삭제", async () => {
        await api(`/api/stock-inbounds/${item.id}`, { method: "DELETE" });
        refresh();
        loadCurrentTab();
      }, "danger-button"));
      list.appendChild(line);
    });
  }
  wrap.append(
    field("입고일", receipt),
    field("채널", channel),
    field("수량", qty),
    actions(button("추가", async () => {
      await api("/api/stock-inbounds", {
        method: "POST",
        body: JSON.stringify({
          receipt_date: compactDate(receipt.value),
          master_id: row.id,
          channel: channel.value,
          quantity: parseInteger(qty.value) || 0,
        }),
      });
      qty.value = "";
      await refresh();
      loadCurrentTab();
    }, "primary-button")),
    list,
  );
  showModal("입고 예정 관리", wrap);
  refresh().catch((error) => {
    list.textContent = error.message;
    setStatus(error.message, true);
  });
}

async function loadChannel(channel, query = "") {
  const q = input("q", query, "search");
  q.placeholder = `${channel === "naver" ? "네이버" : "쿠팡"} 상품명 검색`;
  q.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadChannel(channel, q.value);
  });
  const favoriteFilter = select("favorite", [["all", "전체"], ["favorite", "즐겨찾기"]]);
  favoriteFilter.value = state.channelFavoriteFilter[channel] || "all";
  favoriteFilter.addEventListener("change", () => {
    state.channelFavoriteFilter[channel] = favoriteFilter.value;
    loadChannel(channel, q.value);
  });
  toolbar(
    button("동기화", async () => loadJobs(await api(`/api/channels/${channel}/sync`, { method: "POST" }))),
    button("★", () => toggleFavorite(channel)),
    button("이름수정", () => renameChannelProduct(channel)),
    button("마스터연결", () => showLinkMaster(channel)),
    button("새마스터+연결", () => createMasterFromChannel(channel)),
    button("연결해제", () => unlinkSelectedMaster(channel), "danger-button"),
    button("URL", () => openSelectedProduct()),
    label("필터"),
    favoriteFilter,
    label("검색"),
    q,
    button("검색", () => loadChannel(channel, q.value)),
    spacer(),
  );
  const data = await api(`/api/channels/${channel}?q=${encodeURIComponent(q.value)}`);
  let rows = normalizeRows(data);
  if (favoriteFilter.value === "favorite") rows = rows.filter((row) => row.isFavorite);
  renderTable(rows);
  const total = rows.reduce((sum, row) => {
    const qty = Number(valueFrom(row, ["todaySales", "today_sales"])) || 0;
    const price = Number(valueFrom(row, ["price"])) || 0;
    return sum + qty * price;
  }, 0);
  const warnings = data.warnings?.length ? ` | 경고 ${data.warnings.length}건` : "";
  setStatus(`${channel === "naver" ? "네이버" : "쿠팡"} ${rows.length.toLocaleString("ko-KR")}건 | 오늘 총 판매금액: ${formatPrice(total) || "0원"}${warnings}`);
}

function selectedChannelRow() {
  const row = selectedRequired();
  if (!row) return null;
  const key = productIdentity(row);
  if (!key) {
    setStatus("상품 식별키가 없습니다.", true);
    return null;
  }
  return row;
}

async function patchChannelProduct(channel, row, payload) {
  await api(`/api/channels/${channel}/products/${encodeURIComponent(productIdentity(row))}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

async function toggleFavorite(channel) {
  const row = selectedChannelRow();
  if (!row) return;
  await patchChannelProduct(channel, row, { favorite: !row.isFavorite });
  await loadChannel(channel);
}

async function renameChannelProduct(channel) {
  const row = selectedChannelRow();
  if (!row) return;
  const next = prompt("표시할 상품명", row.customName || row.name || "");
  if (next === null) return;
  await patchChannelProduct(channel, row, { customName: next.trim() || null });
  await loadChannel(channel);
}

async function showLinkMaster(channel) {
  const row = selectedChannelRow();
  if (!row) return;
  const data = await api("/api/masters?include_links=0");
  const masters = normalizeRows(data);
  if (!masters.length) {
    setStatus("연결할 마스터가 없습니다. 먼저 새 마스터를 만드세요.", true);
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const master = select("master", masters.map((item) => [String(item.id), `${item.name} (#${item.id})`]));
  const multiplier = input("multiplier", row.linkMultiplier || 1, "number");
  wrap.append(
    field("마스터", master),
    field("배수", multiplier),
    actions(button("연결", async () => {
      await api("/api/master-links", {
        method: "POST",
        body: JSON.stringify({
          channel,
          product_key: productIdentity(row),
          master_id: Number(master.value),
          multiplier: parseInteger(multiplier.value) || 1,
        }),
      });
      closeModal();
      loadChannel(channel);
    }, "primary-button")),
  );
  showModal("마스터 연결", wrap);
}

async function createMasterFromChannel(channel) {
  const row = selectedChannelRow();
  if (!row) return;
  const name = prompt("새 마스터 이름", row.name || "");
  if (!name) return;
  const created = await api("/api/masters", {
    method: "POST",
    body: JSON.stringify({ name, unit_cost: null, memo: null }),
  });
  const masterId = created.master?.id || created.id || created.data?.master?.id;
  if (!masterId) {
    setStatus("마스터 생성 결과에서 ID를 찾지 못했습니다.", true);
    return;
  }
  await api("/api/master-links", {
    method: "POST",
    body: JSON.stringify({ channel, product_key: productIdentity(row), master_id: masterId, multiplier: 1 }),
  });
  await loadChannel(channel);
}

async function unlinkSelectedMaster(channel) {
  const row = selectedChannelRow();
  if (!row) return;
  await api(`/api/master-links?channel=${encodeURIComponent(channel)}&product_key=${encodeURIComponent(productIdentity(row))}`, { method: "DELETE" });
  await loadChannel(channel);
}

function openSelectedProduct() {
  const row = selectedRequired();
  if (!row) return;
  const url = productUrl(row);
  if (!url) return setStatus("열 수 있는 URL이 없습니다.", true);
  window.open(url, "_blank", "noreferrer");
}

function showChannelProductDetail(channel, row) {
  if (!row) return;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  wrap.append(preBlock(row));
  wrap.append(actions(
    button(row.isFavorite ? "즐겨찾기 해제" : "즐겨찾기", () => toggleFavorite(channel)),
    button("마스터연결", () => showLinkMaster(channel)),
    button("URL 열기", openSelectedProduct),
  ));
  showModal(`${channelLabel(channel)} 상품 상세`, wrap);
}

async function loadSales() {
  const dateInput = input("date", new Date().toISOString().slice(0, 10), "date");
  toolbar(
    label("일자"),
    dateInput,
    button("조회", () => loadSalesWithDate(dateInput.value)),
    button("날짜 목록", async () => renderTable(normalizeRows(await api("/api/sales/dates")))),
    spacer(),
  );
  await loadSalesWithDate(dateInput.value);
}

async function loadSalesWithDate(value) {
  const data = await api(`/api/sales?date=${encodeURIComponent(value)}`);
  const rows = normalizeRows(data.sales ? data : data.data);
  renderTable(rows);
  setStatus(`판매일보 ${rows.length.toLocaleString("ko-KR")}건`);
}

async function loadRevenue(period = "30") {
  const days = select("period_days", [["7", "7일"], ["14", "14일"], ["30", "30일"], ["60", "60일"], ["90", "90일"]]);
  days.value = period;
  toolbar(
    button("동기화", async () => loadJobs(await api(`/api/revenue/sync?period_days=${days.value}`, { method: "POST" }))),
    label("기준기간"),
    days,
    button("조회", () => loadRevenue(days.value)),
    spacer(),
  );
  const data = await api(`/api/revenue?period_days=${days.value}`);
  const rows = normalizeRows(data.snapshot?.products?.length ? { rows: data.snapshot.products } : data.snapshot?.summaries);
  renderTable(rows);
  setStatus(`매출비교 ${rows.length.toLocaleString("ko-KR")}건`);
}

async function loadKeywords(period = "30") {
  const days = select("period_days", [["7", "7일"], ["14", "14일"], ["30", "30일"], ["60", "60일"], ["90", "90일"]]);
  days.value = period;
  toolbar(
    button("동기화", async () => loadJobs(await api(`/api/keywords/sync?period_days=${days.value}`, { method: "POST" }))),
    label("기준기간"),
    days,
    button("조회", () => loadKeywords(days.value)),
    spacer(),
  );
  const data = await api(`/api/keywords?period_days=${days.value}`);
  const rows = normalizeRows(data.snapshot);
  renderTable(rows);
  setStatus(`키워드매출 ${rows.length.toLocaleString("ko-KR")}건`);
}

async function loadPurchases(kind = "records", channelValue = "all") {
  state.currentPurchaseKind = kind;
  state.currentPurchaseChannel = channelValue;
  const channel = select("channel", [["all", "전체"], ["naver", "네이버"], ["coupang", "쿠팡"]]);
  channel.value = channelValue;
  toolbar(
    label("채널"),
    channel,
    button("구매내역", () => loadPurchaseRows("records", channel.value)),
    button("주문", () => loadPurchaseRows("orders", channel.value)),
    button("브라우저 준비", async () => startLoggedJob(api("/api/purchases/crawler/prepare", { method: "POST" }))),
    button("네이버 수집", async () => startLoggedJob(api("/api/purchases/crawl", {
      method: "POST",
      body: JSON.stringify({ channel: "naver", max_pages: 5 }),
    })), "primary-button"),
    button("쿠팡 수집", async () => startLoggedJob(api("/api/purchases/crawl", {
      method: "POST",
      body: JSON.stringify({ channel: "coupang", max_pages: 5 }),
    })), "primary-button"),
    button("HTML 붙여넣기", () => showPasteImport(channel.value)),
    button("상세", () => showPurchaseDetail(selectedRequired())),
    button("URL", openSelectedProduct),
    spacer(),
  );
  await loadPurchaseRows(kind, channel.value);
}

async function loadPurchaseRows(kind, channel) {
  state.currentPurchaseKind = kind;
  state.currentPurchaseChannel = channel;
  const data = await api(`/api/purchases/${kind}?channel=${channel}&limit=2000`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`${kind === "records" ? "구매내역" : "주문"} ${rows.length.toLocaleString("ko-KR")}건`);
}

function showPurchaseDetail(row) {
  if (!row) return;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const summary = document.createElement("div");
  summary.className = "summary-strip";
  summary.innerHTML = `
    <span>${escapeHtml(channelLabel(row.channel))}</span>
    <strong>${escapeHtml(row.order_no || row.orderNo || "-")}</strong>
    <span>${escapeHtml(row.order_date || row.orderDate || "")}</span>
    <span>${escapeHtml(formatPrice(row.payment_total ?? row.amount ?? row.card_amount))}</span>
  `;
  wrap.append(summary, preBlock(row), actions(button("URL 열기", openSelectedProduct)));
  showModal("구매/주문 상세", wrap);
}

function showPasteImport(channel) {
  const form = document.createElement("div");
  const text = document.createElement("textarea");
  text.placeholder = "주문내역 HTML 또는 텍스트";
  form.append(text, button("가져오기", async () => {
    const result = await api("/api/purchases/import/text", {
      method: "POST",
      body: JSON.stringify({ channel, text: text.value }),
    });
    $("#modal").close();
    setStatus(`파싱 ${result.parsed}건, 저장 ${result.saved}건`);
    loadCurrentTab();
  }, "primary-button"));
  showModal("구매내역 가져오기", form);
}

async function loadCards() {
  const categories = await loadCardCategories();
  const start = input("start_date", todayIso(-30), "date");
  const end = input("end_date", todayIso(), "date");
  const category = select("category", [["", "카테고리"], ...categories.map((item) => [item.code, `${item.emoji || ""} ${item.label}`])]);
  toolbar(
    label("시작"),
    start,
    label("종료"),
    end,
    button("조회", () => loadCardRows(start.value, end.value)),
    category,
    button("카테고리저장", () => patchSelectedCard({ category: category.value || null })),
    button("메모", () => editSelectedCardMemo()),
    button("검토", () => toggleSelectedCardReview()),
    button("카드 동기화", async () => loadJobs(await api("/api/cards/sync", {
      method: "POST",
      body: JSON.stringify({ start_date: start.value, end_date: end.value }),
    })), "primary-button"),
    button("쿠팡매칭", async () => loadJobs(await api("/api/cards/coupang-match", {
      method: "POST",
      body: JSON.stringify({ start_date: start.value, end_date: end.value }),
    }))),
    button("고정비", showFixedCosts),
    spacer(),
  );
  await loadCardRows(start.value, end.value);
}

async function loadCardCategories() {
  if (state.cardCategories) return state.cardCategories;
  state.cardCategories = normalizeRows(await api("/api/cards/categories"));
  return state.cardCategories;
}

async function loadCardRows(start, end) {
  const data = await api(`/api/cards/usages?start_date=${start}&end_date=${end}&limit=5000`);
  const rows = normalizeRows(data);
  renderTable(rows);
  setStatus(`카드사용내역 ${rows.length.toLocaleString("ko-KR")}건`);
}

function cardUsageId(row) {
  return compactValue(valueFrom(row, ["id", "use_key", "useKey"]));
}

async function patchSelectedCard(payload) {
  const row = selectedRequired();
  if (!row) return;
  const id = cardUsageId(row);
  if (!id) return setStatus("카드 사용내역 ID가 없습니다.", true);
  await api(`/api/cards/usages/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  await loadCards();
}

async function editSelectedCardMemo() {
  const row = selectedRequired();
  if (!row) return;
  const memo = prompt("메모", row.memo || "");
  if (memo === null) return;
  await patchSelectedCard({ memo, clear_memo: memo.trim() === "" });
}

async function toggleSelectedCardReview() {
  const row = selectedRequired();
  if (!row) return;
  await patchSelectedCard({ reviewed: !Boolean(row.reviewed) });
}

function showCardUsageEditor(row) {
  if (!row) return;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  wrap.append(preBlock(row), actions(
    button("메모", editSelectedCardMemo),
    button(Boolean(row.reviewed) ? "검토해제" : "검토완료", toggleSelectedCardReview),
  ));
  showModal("카드 사용 상세", wrap);
}

async function showFixedCosts() {
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const list = document.createElement("div");
  list.className = "linked-list";
  const name = input("name");
  name.placeholder = "고정비명";
  const amount = input("amount", "", "number");
  amount.placeholder = "금액";
  const memo = input("memo");
  memo.placeholder = "메모";
  async function refresh() {
    const rows = normalizeRows(await api("/api/cards/fixed-costs"));
    list.innerHTML = rows.length ? "" : `<div class="empty-cell">고정비가 없습니다.</div>`;
    rows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "linked-item";
      item.innerHTML = `<strong>${escapeHtml(row.name || row.title || "-")}</strong> ${escapeHtml(formatPrice(row.amount))} ${escapeHtml(row.memo || "")}`;
      if (row.id) {
        item.append(button("삭제", async () => {
          await api(`/api/cards/fixed-costs/${row.id}`, { method: "DELETE" });
          refresh();
        }, "danger-button"));
      }
      list.appendChild(item);
    });
  }
  wrap.append(
    field("이름", name),
    field("금액", amount),
    field("메모", memo),
    actions(button("저장", async () => {
      await api("/api/cards/fixed-costs", {
        method: "POST",
        body: JSON.stringify({ items: [{ name: name.value, amount: parseInteger(amount.value), memo: memo.value }] }),
      });
      name.value = "";
      amount.value = "";
      memo.value = "";
      refresh();
    }, "primary-button")),
    list,
  );
  showModal("고정비 관리", wrap);
  refresh().catch((error) => {
    list.textContent = error.message;
    setStatus(error.message, true);
  });
}

async function loadFassto(sectionValue = state.fasstoSection) {
  state.fasstoSection = sectionValue;
  const section = select("section", [
    ["goods", "상품"],
    ["stock", "재고"],
    ["warehousing", "입고"],
    ["delivery", "출고"],
    ["parcels", "택배출고"],
    ["revenue", "매출분석"],
  ]);
  section.value = state.fasstoSection;
  const start = input("start", yyyyMmDd(-30), "date");
  const end = input("end", yyyyMmDd(0), "date");
  section.addEventListener("change", () => loadFassto(section.value));
  toolbar(
    label("구분"),
    section,
    label("시작"),
    start,
    label("종료"),
    end,
    button("조회", () => loadFasstoSection(section.value, start.value, end.value)),
    button("상세", showFasstoDetail),
    button("생성", () => showFasstoJsonAction("생성", "POST")),
    button("수정", () => showFasstoJsonAction("수정", "PATCH")),
    button("취소", () => cancelFasstoSelected(), "danger-button"),
    button("명세서", showFasstoStatement),
    button("CSV 저장", () => {
      window.location.href = `/api/fassto/${section.value}?start=${compactDate(start.value)}&end=${compactDate(end.value)}&download=true`;
    }),
    spacer(),
  );
  await loadFasstoSection(section.value, start.value, end.value);
}

function yyyyMmDd(deltaDays) {
  const d = new Date();
  d.setDate(d.getDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function compactDate(value) {
  return String(value || "").replaceAll("-", "");
}

async function loadFasstoSection(section, start, end) {
  state.fasstoSection = section;
  const data = await api(`/api/fassto/${section}?start=${compactDate(start)}&end=${compactDate(end)}`);
  const rows = normalizeRows(data);
  renderTable(rows);
  const sectionLabel = { goods: "상품", stock: "재고", warehousing: "입고", delivery: "출고", parcels: "택배출고", revenue: "매출분석" }[section] || "파스토";
  setStatus(`${sectionLabel} ${rows.length.toLocaleString("ko-KR")}건`);
}

function fasstoSlip(row = selectedRow()) {
  return compactValue(valueFrom(row, ["slipNo", "slip_no", "slip_no"]));
}

function fasstoWritableSection() {
  if (state.fasstoSection === "warehousing") return "warehousing";
  if (state.fasstoSection === "delivery") return "delivery";
  setStatus("입고 또는 출고 탭에서 사용할 수 있습니다.", true);
  return "";
}

async function showFasstoDetail() {
  const section = fasstoWritableSection();
  if (!section) return;
  const slipNo = fasstoSlip();
  if (!slipNo) return setStatus("전표번호가 없습니다.", true);
  const data = await api(`/api/fassto/${section}/${encodeURIComponent(slipNo)}`);
  showModal("파스토 상세", preBlock(data));
}

function defaultFasstoPayload(method) {
  const row = selectedRow() || {};
  if (state.fasstoSection === "warehousing") {
    return {
      items: [{
        slipNo: method === "PATCH" ? fasstoSlip(row) : undefined,
        ordDt: compactDate(todayIso()),
        inPlanDt: compactDate(todayIso()),
        whCd: row.whCd || "",
        supCd: row.supCd || "",
        remark: row.remark || "",
        goods: [{ cstGodCd: row.cstGodCd || "", ordQty: 1 }],
      }],
    };
  }
  return {
    items: [{
      slipNo: method === "PATCH" ? fasstoSlip(row) : undefined,
      ordDt: compactDate(todayIso()),
      outDt: compactDate(todayIso()),
      outDiv: row.outDiv || "1",
      mallNm: row.mallNm || row.salesChannel || "",
      rcvrNm: row.rcvrNm || "",
      rcvrTel: row.rcvrTel || "",
      addr: row.addr || "",
      goods: [{ cstGodCd: row.cstGodCd || "", ordQty: 1 }],
    }],
  };
}

function showFasstoJsonAction(title, method) {
  const section = fasstoWritableSection();
  if (!section) return;
  const wrap = document.createElement("div");
  wrap.className = "stack";
  const textarea = document.createElement("textarea");
  textarea.value = JSON.stringify(defaultFasstoPayload(method), null, 2);
  wrap.append(textarea, actions(button(title, async () => {
    let payload;
    try {
      payload = JSON.parse(textarea.value || "{}");
    } catch (error) {
      setStatus(`JSON 오류: ${error.message}`, true);
      return;
    }
    await api(`/api/fassto/${section}`, { method, body: JSON.stringify(payload) });
    closeModal();
    loadFassto(section);
  }, "primary-button")));
  showModal(`파스토 ${title}`, wrap);
}

async function cancelFasstoSelected() {
  const section = fasstoWritableSection();
  if (!section) return;
  const slipNo = fasstoSlip();
  if (!slipNo) return setStatus("취소할 전표번호가 없습니다.", true);
  if (!confirm(`${slipNo} 전표를 취소할까요?`)) return;
  await api(`/api/fassto/${section}/cancel`, {
    method: "POST",
    body: JSON.stringify({ items: [{ slipNo }] }),
  });
  await loadFassto(section);
}

function showFasstoStatement() {
  const row = selectedRequired();
  if (!row) return;
  const wrap = document.createElement("div");
  wrap.className = "statement-preview";
  wrap.innerHTML = `
    <h3>거래명세서</h3>
    <p>전표번호 ${escapeHtml(fasstoSlip(row) || "-")}</p>
    <table>
      <tbody>
        <tr><th>일자</th><td>${escapeHtml(row.ordDt || row.outDt || row.inPlanDt || "")}</td></tr>
        <tr><th>거래처</th><td>${escapeHtml(row.supNm || row.mallNm || row.salesChannel || "")}</td></tr>
        <tr><th>수량</th><td>${escapeHtml(formatNumber(sumGoods(row, ["ordQty", "inQty", "outQty"])))}</td></tr>
      </tbody>
    </table>
  `;
  wrap.append(actions(button("인쇄", () => window.print(), "primary-button")));
  showModal("명세서 미리보기", wrap);
}

async function syncAll() {
  await startLoggedJob(api("/api/sync/all", { method: "POST" }));
}

document.querySelectorAll(".tab-button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tab = btn.dataset.tab;
    loadCurrentTab();
  });
});

$("#refreshBtn").addEventListener("click", loadCurrentTab);
$("#syncBtn").addEventListener("click", syncAll);
$("#fontBtn").addEventListener("click", () => setStatus("현재 글꼴: Malgun Gothic / Apple SD Gothic Neo"));
$("#piStatusBtn").addEventListener("click", async () => {
  const health = await api("/api/health");
  const configured = health.config?.monitorConfigured ? "연결 설정됨" : "미설정";
  setStatus(`라즈베리파이: ${configured}`);
});

api("/api/config/status")
  .then((config) => {
    state.config = config;
    $("#runtimeLabel").textContent = config.vercel ? "Vercel" : "Local/Web";
    $("#piStatusBtn").style.display = config.monitorConfigured ? "" : "none";
  })
  .catch(() => {});

loadCurrentTab();
