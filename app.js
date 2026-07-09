/* Daily Market Brief — 대시보드 렌더링 (거시 + 산업별 주가 + 산업별 선행지표) */

const PAGES = {
  macro: {
    file: "data.json",
    categories: ["이격도", "환율", "금리", "위험·변동성", "원자재", "기타"],
    showRegime: true,
    metaLabel: "기준",
  },
  sector: {
    file: "sector.json",
    categories: ["반도체", "조선", "소프트웨어"],
    showRegime: false,
    metaLabel: "산업 주가 기준",
  },
  leading: {
    file: "leading.json",
    categories: ["반도체", "조선", "소프트웨어"],
    showRegime: false,
    metaLabel: "산업별 선행지표 기준",
  },
  flow: {
    file: "flow.json",
    categories: ["해외", "기관", "개인"],
    showRegime: false,
    metaLabel: "투자자 수급 기준",
  },
  us_flow: {
    file: "us_flow.json",
    categories: ["S&P 500", "NASDAQ 100"],
    showRegime: false,
    metaLabel: "미국 지수 수급 기준 (CFTC COT)",
  },
  liquidity: {
    file: "liquidity.json",
    categories: ["미국", "한국"],
    showRegime: false,
    metaLabel: "시장 유동성 기준",
  },
};

const SIG_COLORS = ["#2ecc71", "#f1c40f", "#e67e22", "#e74c3c"];

let currentPage = "macro";
const PAGE_DATA = { macro: null, sector: null, leading: null, flow: null, us_flow: null, liquidity: null };
let modalChart = null;
const sparkCharts = [];

document.addEventListener("DOMContentLoaded", init);

async function init() {
  try {
    PAGE_DATA.macro = await loadJson(PAGES.macro.file);
  } catch (e) {
    document.getElementById("generated-at").textContent = "data.json을 불러오지 못했습니다.";
    console.error(e);
    return;
  }

  try {
    PAGE_DATA.sector = await loadJson(PAGES.sector.file);
  } catch (e) {
    console.warn("sector.json 없음 또는 로딩 실패", e);
  }

  try {
    PAGE_DATA.leading = await loadJson(PAGES.leading.file);
  } catch (e) {
    console.warn("leading.json 없음 또는 로딩 실패", e);
  }

  try {
    PAGE_DATA.flow = await loadJson(PAGES.flow.file);
  } catch (e) {
    console.warn("flow.json 없음 또는 로딩 실패", e);
  }

  try {
    PAGE_DATA.us_flow = await loadJson(PAGES.us_flow.file);
  } catch (e) {
    console.warn("us_flow.json 없음 또는 로딩 실패", e);
  }

  try {
    PAGE_DATA.liquidity = await loadJson(PAGES.liquidity.file);
  } catch (e) {
    console.warn("liquidity.json 없음 또는 로딩 실패", e);
  }

  const macro = PAGE_DATA.macro || {};
  document.getElementById("session-badge").textContent = macro.session || "브리핑";
  document.getElementById("generated-at").textContent = "기준: " + (macro.generated_at || "");

  renderRegime(macro.regime);
  setupPageTabs();
  setupModal();
  renderPage("macro");
}

async function loadJson(file) {
  const res = await fetch(file + "?t=" + Date.now());
  if (!res.ok) throw new Error(file + " 응답 " + res.status);
  return res.json();
}

function setupPageTabs() {
  document.querySelectorAll(".page-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nextPage = btn.dataset.page;
      if (!PAGES[nextPage] || nextPage === currentPage) return;
      document.querySelectorAll(".page-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderPage(nextPage);
    });
  });
}

function renderPage(page) {
  currentPage = page;
  const cfg = PAGES[page] || PAGES.macro;
  const data = PAGE_DATA[page];
  const regimeEl = document.getElementById("regime");
  const meta = document.getElementById("sector-meta");
  const nav = document.getElementById("category-nav");
  const grid = document.getElementById("indicators");

  regimeEl.style.display = cfg.showRegime ? "" : "none";

  if (page === "macro") {
    meta.style.display = "none";
  } else if (data) {
    meta.textContent = cfg.metaLabel + ": " + (data.generated_at || "");
    meta.style.display = "";
  } else {
    meta.textContent = cfg.metaLabel + ": 데이터를 불러오지 못했습니다.";
    meta.style.display = "";
  }

  if (!data || !Array.isArray(data.indicators) || !data.indicators.length) {
    nav.innerHTML = "";
    while (sparkCharts.length) sparkCharts.pop().destroy();
    grid.innerHTML = '<div class="cat-group-title">표시할 지표 데이터가 없습니다.</div>';
    return;
  }

  renderNav(cfg, data);
  renderIndicators(cfg, data, "전체");
}

function renderRegime(r) {
  if (!r) return;
  const el = document.getElementById("regime");

  const scoreDefs = [
    ["위험도", "risk", "neg"],
    ["과열도", "overheating", "neg"],
    ["시장 폭", "breadth", "pos"],
    ["환율 스트레스", "fx_stress", "neg"],
    ["신용·변동성 안정", "stability", "pos"],
    ["신규 진입 매력", "entry", "pos"],
    ["공포매수 점수", "fear_buy", "pos"],
    ["과열축소 점수", "mania_reduce", "neg"],
  ];

  const scoreHtml = scoreDefs
    .map(([name, key, dir]) => {
      const v = r.scores && r.scores[key];
      if (v === undefined || v === null) return "";
      const pct = Math.max(0, Math.min(100, Number(v)));
      const hue = dir === "pos" ? pct * 1.2 : (100 - pct) * 1.2;
      return `<div class="score-item">
        <div class="score-name">${name}<span class="score-val">${escapeHtml(v)}</span></div>
        <div class="score-bar-bg"><div class="score-bar-fill" style="width:${pct}%;background:hsl(${hue},70%,50%)"></div></div>
      </div>`;
    })
    .join("");

  const drivers = (r.key_drivers || []).map((d) => `<li>${escapeHtml(d)}</li>`).join("");
  const risks = (r.risks || []).map((d) => `<li>${escapeHtml(d)}</li>`).join("");

  el.innerHTML = `
    <div class="regime-head">
      <span class="regime-label">${escapeHtml(r.final_label || "")}</span>
      <span class="regime-action">${escapeHtml(r.action_label || "")}</span>
    </div>
    ${r.headline ? `<p class="regime-headline">${escapeHtml(r.headline)}</p>` : ""}
    ${r.one_liner ? `<p class="regime-oneliner">${escapeHtml(r.one_liner)}</p>` : ""}
    <div class="score-grid">${scoreHtml}</div>
    <div class="driver-block">
      ${drivers ? `<div><h3>핵심 동인</h3><ul>${drivers}</ul></div>` : ""}
      ${risks ? `<div><h3>리스크</h3><ul>${risks}</ul></div>` : ""}
    </div>`;
}

function renderNav(cfg, data) {
  const cats = ["전체", ...cfg.categories.filter((c) => data.indicators.some((i) => i.category === c))];
  const nav = document.getElementById("category-nav");
  nav.innerHTML = cats
    .map((c, idx) => `<button class="cat-btn${idx === 0 ? " active" : ""}" data-cat="${escapeHtml(c)}">${escapeHtml(c)}</button>`)
    .join("");

  nav.querySelectorAll(".cat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      nav.querySelectorAll(".cat-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderIndicators(cfg, data, btn.dataset.cat);
    });
  });
}

function appendGroupTitle(grid, text) {
  const title = document.createElement("div");
  title.className = "cat-group-title";
  title.textContent = text;
  grid.appendChild(title);
}

function renderIndicators(cfg, data, filterCat) {
  while (sparkCharts.length) sparkCharts.pop().destroy();

  const grid = document.getElementById("indicators");
  grid.innerHTML = "";

  const cats = cfg.categories.filter((c) =>
    data.indicators.some((i) => i.category === c && (filterCat === "전체" || i.category === filterCat))
  );

  cats.forEach((cat) => {
    const items = data.indicators.filter(
      (i) => i.category === cat && (filterCat === "전체" || i.category === filterCat)
    );
    if (!items.length) return;
    if (filterCat === "전체") appendGroupTitle(grid, cat);
    items.forEach((ind) => grid.appendChild(makeCard(ind)));
  });

  data.indicators.forEach((ind) => {
    if (filterCat !== "전체" && ind.category !== filterCat) return;
    const canvas = document.getElementById("spark-" + cssId(ind.name));
    if (canvas && ind.chart) drawSparkline(canvas, ind);
  });

  if (!grid.children.length) {
    grid.innerHTML = '<div class="cat-group-title">표시할 지표가 없습니다.</div>';
  }
}

function makeCard(ind) {
  const card = document.createElement("div");
  const hasChart = !!ind.chart;
  card.className = `card sig-${Number(ind.signal_level || 0)}${hasChart ? "" : " no-chart"}`;
  const freqBadge = ind.freq ? `<span class="freq-badge">${escapeHtml(ind.freq)}</span>` : "";
  card.innerHTML = `
    <div class="card-top">
      <span class="card-name">${escapeHtml(ind.name)}${freqBadge}</span>
      <span class="card-state state-${Number(ind.signal_level || 0)}">${escapeHtml(ind.state || "")}</span>
    </div>
    <div class="card-value">${ind.ok === false ? "수집 실패" : escapeHtml(ind.value_text || "—")}</div>
    <div class="card-change">${escapeHtml(ind.change_text || "")}</div>
    ${hasChart ? `<div class="card-spark"><canvas id="spark-${cssId(ind.name)}"></canvas></div>` : ""}
    <div class="card-date">${escapeHtml(ind.date || "")} · ${escapeHtml(ind.source || "")}</div>`;
  if (hasChart) card.addEventListener("click", () => openModal(ind));
  return card;
}

function primarySeries(chart) {
  if (!chart || !chart.series) return [];
  if (chart.type === "disparity") return chart.series.disparity || [];
  if (chart.type === "multi_ma") return chart.series.close || chart.series.ma20 || chart.series.ma5 || [];
  if (chart.type === "flow_daily") return chart.series.cum || chart.series.value || [];
  return chart.series.value || [];
}

function drawSparkline(canvas, ind) {
  const data = primarySeries(ind.chart);
  if (!Array.isArray(data) || !data.length) return;
  const color = SIG_COLORS[Number(ind.signal_level || 0)] || "#4a9eff";
  const c = new Chart(canvas, {
    type: "line",
    data: {
      labels: ind.chart.labels || [],
      datasets: [{ data, borderColor: color, borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.2 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
      elements: { line: { borderCapStyle: "round" } },
    },
  });
  sparkCharts.push(c);
}

function setupModal() {
  const modal = document.getElementById("modal");
  document.getElementById("modal-close").addEventListener("click", closeModal);
  modal.querySelector(".modal-backdrop").addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
}

function openModal(ind) {
  document.getElementById("modal-title").textContent = ind.name;
  document.getElementById("modal-meta").textContent =
    `${ind.value_text || ""}  ·  ${ind.state || ""}  ·  ${ind.change_text || ""}  ·  ${ind.date || ""}`;
  document.getElementById("modal-comment").textContent = ind.comment || "";
  document.getElementById("modal-mdd").textContent = ind.mdd_line || "";

  renderFlowDetail(ind);

  if (modalChart) modalChart.destroy();
  modalChart = buildModalChart(ind);

  document.getElementById("modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

function buildModalChart(ind) {
  const ctx = document.getElementById("modal-chart");
  const chart = ind.chart;
  const color = SIG_COLORS[Number(ind.signal_level || 0)] || "#4a9eff";
  const datasets = [];

  if (chart.type === "disparity") {
    datasets.push({
      label: "이격도",
      data: chart.series.disparity || [],
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    });
    const refDefs = [
      ["강세 " + (chart.refs && chart.refs.strong), chart.refs && chart.refs.strong, "#5a6472"],
      ["과열 " + (chart.refs && chart.refs.overheat), chart.refs && chart.refs.overheat, "#e67e22"],
      ["극단 " + (chart.refs && chart.refs.extreme), chart.refs && chart.refs.extreme, "#e74c3c"],
    ];
    refDefs.forEach(([label, val, col]) => {
      if (val === undefined || val === null) return;
      datasets.push({ label, data: (chart.labels || []).map(() => val), borderColor: col, borderWidth: 1, borderDash: [5, 4], pointRadius: 0 });
    });
  } else if (chart.type === "multi_ma") {
    const defs = [
      ["종가", "close", color, 2],
      ["5일선", "ma5", "#8ab4f8", 1.4],
      ["20일선", "ma20", "#fbbc04", 1.4],
      ["60일선", "ma60", "#34a853", 1.4],
      ["120일선", "ma120", "#ea4335", 1.4],
    ];
    defs.forEach(([label, key, col, width]) => {
      if (!chart.series[key]) return;
      datasets.push({ label, data: chart.series[key], borderColor: col, borderWidth: width, pointRadius: 0, tension: 0.15 });
    });
  } else if (chart.type === "flow_daily") {
    const daily = chart.series.value || [];
    const cum = chart.series.cum || [];
    // 단위별 라벨: 억원(KRX 수급)=일별/누적 순매수, 계약(CFTC COT)=주간 변화/순포지션 레벨
    const unit = chart.unit || "억원";
    const isContracts = unit === "계약";
    const barLabel = isContracts ? `주간 변화(${unit})` : `일별 순매수(${unit})`;
    const lineLabel = isContracts ? `순포지션(${unit})` : `누적 순매수(${unit})`;
    const barColors = daily.map((v) => (v >= 0 ? "rgba(46,204,113,0.45)" : "rgba(231,76,60,0.45)"));
    datasets.push({
      type: "bar",
      label: barLabel,
      data: daily,
      backgroundColor: barColors,
      borderWidth: 0,
      order: 2,
    });
    const cumColor = cum.length && cum[cum.length - 1] >= 0 ? "#2ecc71" : "#e74c3c";
    datasets.push({
      type: "line",
      label: lineLabel,
      data: cum,
      borderColor: cumColor,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
      order: 1,
    });
  } else {
    datasets.push({
      label: ind.name,
      data: chart.series.value || [],
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    });
  }

  return new Chart(ctx, {
    type: "line",
    data: { labels: chart.labels || [], datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, labels: { color: "#9aa4b2", boxWidth: 18, font: { size: 11 } } },
        tooltip: { enabled: true },
      },
      scales: {
        x: { ticks: { color: "#6b7480", maxTicksLimit: 8, font: { size: 10 } }, grid: { color: "#222a35" } },
        y: { ticks: { color: "#6b7480", font: { size: 10 } }, grid: { color: "#222a35" } },
      },
    },
  });
}

function jsFmtWon(x) {
  if (x == null || !isFinite(x)) return "—";
  const sign = x >= 0 ? "+" : "-";
  const v = Math.abs(Number(x));
  const jo = 1e12, eok = 1e8;
  if (v >= jo) return sign + (v / jo).toFixed(2) + "조";
  if (v >= eok) return sign + Math.round(v / eok).toLocaleString() + "억";
  return sign + Math.round(v).toLocaleString() + "원";
}

function renderFlowDetail(ind) {
  const el = document.getElementById("modal-flow");
  if (!el) return;
  const buys = ind.top_buys || [];
  const sells = ind.top_sells || [];
  const sectors = ind.sector_flow || [];
  if (!buys.length && !sells.length && !sectors.length) {
    el.innerHTML = "";
    return;
  }

  const rankRows = (arr, cls) => {
    const maxAbs = Math.max(1, ...arr.map((x) => Math.abs(x.net_value)));
    return arr
      .map((x) => {
        const w = Math.max(3, (Math.abs(x.net_value) / maxAbs) * 100);
        const sec = x.sector ? `<span class="flow-sec">${escapeHtml(x.sector)}</span>` : "";
        return `<div class="flow-row">
          <span class="flow-nm">${escapeHtml(x.name)}${sec}</span>
          <span class="flow-bar-wrap"><span class="flow-bar ${cls}" style="width:${w}%"></span></span>
          <span class="flow-amt ${cls}">${escapeHtml(jsFmtWon(x.net_value))}</span>
        </div>`;
      })
      .join("");
  };

  const secMax = Math.max(1, ...sectors.map((x) => Math.abs(x.net_value)));
  const secRows = sectors
    .map((x) => {
      const pos = x.net_value >= 0;
      const w = (Math.abs(x.net_value) / secMax) * 50;
      const barStyle = pos ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
      return `<div class="sec-row">
        <span class="sec-nm">${escapeHtml(x.sector)}</span>
        <span class="sec-track"><span class="sec-bar ${pos ? "buy" : "sell"}" style="${barStyle}"></span></span>
        <span class="flow-amt ${pos ? "buy" : "sell"}">${escapeHtml(jsFmtWon(x.net_value))}</span>
      </div>`;
    })
    .join("");

  el.innerHTML = `
    <div class="flow-cols">
      <div class="flow-col">
        <h4 class="flow-h buy">순매수 상위 (돈 유입)</h4>
        ${buys.length ? rankRows(buys, "buy") : '<p class="flow-empty">없음</p>'}
      </div>
      <div class="flow-col">
        <h4 class="flow-h sell">순매도 상위 (돈 유출)</h4>
        ${sells.length ? rankRows(sells, "sell") : '<p class="flow-empty">없음</p>'}
      </div>
    </div>
    ${sectors.length ? `<h4 class="flow-h">섹터별 순매수 흐름</h4><div class="sec-list">${secRows}</div>` : ""}
  `;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function cssId(name) {
  return encodeURIComponent(name).replace(/%/g, "_");
}
