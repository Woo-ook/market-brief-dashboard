/* Daily Market Brief — 대시보드 렌더링 */

const CATEGORY_ORDER = ["이격도", "환율", "금리", "위험·변동성", "기타"];
const SIG_COLORS = ["#2ecc71", "#f1c40f", "#e67e22", "#e74c3c"];

let DATA = null;
let modalChart = null;
const sparkCharts = [];

document.addEventListener("DOMContentLoaded", init);

async function init() {
  try {
    const res = await fetch("data.json?t=" + Date.now());
    if (!res.ok) throw new Error("data.json 응답 " + res.status);
    DATA = await res.json();
  } catch (e) {
    document.getElementById("generated-at").textContent = "데이터를 불러오지 못했습니다.";
    console.error(e);
    return;
  }

  document.getElementById("session-badge").textContent = DATA.session || "브리핑";
  document.getElementById("generated-at").textContent = "기준: " + (DATA.generated_at || "");

  renderRegime(DATA.regime);
  renderNav();
  renderIndicators("전체");
  setupModal();
}

/* ── 국면 요약 ── */
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
      const v = r.scores[key];
      if (v === undefined || v === null) return "";
      const pct = Math.max(0, Math.min(100, v));
      // pos: 높을수록 초록 / neg: 높을수록 빨강
      const hue = dir === "pos" ? pct * 1.2 : (100 - pct) * 1.2;
      return `<div class="score-item">
        <div class="score-name">${name}<span class="score-val">${v}</span></div>
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

/* ── 카테고리 네비 ── */
function renderNav() {
  const cats = ["전체", ...CATEGORY_ORDER.filter((c) => DATA.indicators.some((i) => i.category === c))];
  const nav = document.getElementById("category-nav");
  nav.innerHTML = cats
    .map((c, idx) => `<button class="cat-btn${idx === 0 ? " active" : ""}" data-cat="${c}">${c}</button>`)
    .join("");
  nav.querySelectorAll(".cat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      nav.querySelectorAll(".cat-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderIndicators(btn.dataset.cat);
    });
  });
}

/* ── 지표 카드 ── */
function renderIndicators(filterCat) {
  // 기존 스파크라인 정리
  while (sparkCharts.length) sparkCharts.pop().destroy();

  const grid = document.getElementById("indicators");
  grid.innerHTML = "";

  const cats = CATEGORY_ORDER.filter((c) =>
    DATA.indicators.some((i) => i.category === c && (filterCat === "전체" || i.category === filterCat))
  );

  cats.forEach((cat) => {
    const items = DATA.indicators.filter((i) => i.category === cat);
    if (!items.length) return;
    if (filterCat === "전체") {
      const title = document.createElement("div");
      title.className = "cat-group-title";
      title.textContent = cat;
      grid.appendChild(title);
    }
    items.forEach((ind) => grid.appendChild(makeCard(ind)));
  });

  // 카드 그려진 뒤 스파크라인 생성
  DATA.indicators.forEach((ind) => {
    const canvas = document.getElementById("spark-" + cssId(ind.name));
    if (canvas && ind.chart) drawSparkline(canvas, ind);
  });
}

function makeCard(ind) {
  const card = document.createElement("div");
  const hasChart = !!ind.chart;
  card.className = `card sig-${ind.signal_level}${hasChart ? "" : " no-chart"}`;
  card.innerHTML = `
    <div class="card-top">
      <span class="card-name">${escapeHtml(ind.name)}</span>
      <span class="card-state state-${ind.signal_level}">${escapeHtml(ind.state || "")}</span>
    </div>
    <div class="card-value">${ind.ok ? escapeHtml(ind.value_text || "—") : "수집 실패"}</div>
    <div class="card-change">${escapeHtml(ind.change_text || "")}</div>
    ${hasChart ? `<div class="card-spark"><canvas id="spark-${cssId(ind.name)}"></canvas></div>` : ""}
    <div class="card-date">${escapeHtml(ind.date || "")} · ${escapeHtml(ind.source || "")}</div>`;
  if (hasChart) card.addEventListener("click", () => openModal(ind));
  return card;
}

function primarySeries(chart) {
  return chart.type === "disparity" ? chart.series.disparity : chart.series.value;
}

function drawSparkline(canvas, ind) {
  const data = primarySeries(ind.chart);
  const color = SIG_COLORS[ind.signal_level] || "#4a9eff";
  const c = new Chart(canvas, {
    type: "line",
    data: {
      labels: ind.chart.labels,
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

/* ── 모달 (전체 차트) ── */
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
  const color = SIG_COLORS[ind.signal_level] || "#4a9eff";

  const datasets = [];
  if (chart.type === "disparity") {
    datasets.push({
      label: "이격도",
      data: chart.series.disparity,
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    });
    const refDefs = [
      ["강세 " + chart.refs.strong, chart.refs.strong, "#5a6472"],
      ["과열 " + chart.refs.overheat, chart.refs.overheat, "#e67e22"],
      ["극단 " + chart.refs.extreme, chart.refs.extreme, "#e74c3c"],
    ];
    refDefs.forEach(([label, val, col]) => {
      datasets.push({
        label,
        data: chart.labels.map(() => val),
        borderColor: col,
        borderWidth: 1,
        borderDash: [5, 4],
        pointRadius: 0,
      });
    });
  } else {
    datasets.push({
      label: ind.name,
      data: chart.series.value,
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    });
  }

  return new Chart(ctx, {
    type: "line",
    data: { labels: chart.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: chart.type === "disparity", labels: { color: "#9aa4b2", boxWidth: 18, font: { size: 11 } } },
        tooltip: { enabled: true },
      },
      scales: {
        x: { ticks: { color: "#6b7480", maxTicksLimit: 8, font: { size: 10 } }, grid: { color: "#222a35" } },
        y: { ticks: { color: "#6b7480", font: { size: 10 } }, grid: { color: "#222a35" } },
      },
    },
  });
}

/* ── 유틸 ── */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
function cssId(name) {
  return encodeURIComponent(name).replace(/%/g, "_");
}
