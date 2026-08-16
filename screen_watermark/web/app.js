const fileInput = document.querySelector("#fileInput");
const canvas = document.querySelector("#photoCanvas");
const emptyState = document.querySelector("#emptyState");
const pointList = document.querySelector("#pointList");
const pointSection = document.querySelector("#pointSection");
const traceModeInputs = document.querySelectorAll('input[name="traceMode"]');
const undoBtn = document.querySelector("#undoBtn");
const resetBtn = document.querySelector("#resetBtn");
const decodeBtn = document.querySelector("#decodeBtn");
const tilePx = document.querySelector("#tilePx");
const offsetStep = document.querySelector("#offsetStep");
const resultPanel = document.querySelector("#resultPanel");
const resultList = document.querySelector("#resultList");
const statusBadge = document.querySelector("#statusBadge");
const rectifiedPanel = document.querySelector("#rectifiedPanel");
const rectifiedImage = document.querySelector("#rectifiedImage");
const payloadInput = document.querySelector("#payloadInput");
const subjectInput = document.querySelector("#subjectInput");
const deviceInput = document.querySelector("#deviceInput");
const sessionInput = document.querySelector("#sessionInput");
const saveRegistryBtn = document.querySelector("#saveRegistryBtn");
const successRate = document.querySelector("#successRate");
const recentRate = document.querySelector("#recentRate");
const statsDetail = document.querySelector("#statsDetail");
const historyList = document.querySelector("#historyList");

const ctx = canvas.getContext("2d");
const defaults = {
  tilePx: "256",
  offsetStep: "32",
};

const state = {
  imageId: null,
  image: null,
  naturalWidth: 0,
  naturalHeight: 0,
  scale: 1,
  points: [],
  traceMode: "auto",
};

function updateDecodeButton() {
  const pointCount = state.points.length;
  const canDecode = Boolean(state.imageId) && (state.traceMode === "manual" ? pointCount === 4 : true);
  decodeBtn.disabled = !canDecode;
  undoBtn.disabled = state.traceMode !== "manual" || pointCount === 0;
  resetBtn.disabled = state.traceMode !== "manual" || pointCount === 0;
  pointSection.classList.toggle("inactive", state.traceMode !== "manual");
  canvas.classList.toggle("is-selecting", state.traceMode === "manual");
  if (state.traceMode === "manual" && pointCount < 4) {
    decodeBtn.textContent = "继续选四角";
  } else if (state.traceMode === "manual") {
    decodeBtn.textContent = "手动溯源";
  } else if (state.traceMode === "partial") {
    decodeBtn.textContent = "部分屏溯源";
  } else {
    decodeBtn.textContent = "全屏自动溯源";
  }
}

function clearResult() {
  resultList.innerHTML = "";
  statusBadge.textContent = "等待";
  statusBadge.className = "badge";
  rectifiedImage.removeAttribute("src");
  resultPanel.classList.add("hidden");
  rectifiedPanel.classList.add("hidden");
}

function resetInputsForNewImage() {
  state.traceMode = "auto";
  state.points = [];
  traceModeInputs.forEach((input) => {
    input.checked = input.value === state.traceMode;
  });
  tilePx.value = defaults.tilePx;
  offsetStep.value = defaults.offsetStep;
  payloadInput.value = "";
  subjectInput.value = "";
  deviceInput.value = "";
  sessionInput.value = "";
  clearResult();
  renderPoints();
}

function clearImageBeforeUpload() {
  state.imageId = null;
  state.image = null;
  state.naturalWidth = 0;
  state.naturalHeight = 0;
  state.scale = 1;
  canvas.width = 0;
  canvas.height = 0;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  emptyState.classList.remove("hidden");
  updateDecodeButton();
}

function fitCanvas() {
  if (!state.image) return;
  const maxWidth = canvas.parentElement.clientWidth - 32;
  const maxHeight = window.innerHeight - 130;
  state.scale = Math.min(1, maxWidth / state.naturalWidth, maxHeight / state.naturalHeight);
  canvas.width = Math.round(state.naturalWidth * state.scale);
  canvas.height = Math.round(state.naturalHeight * state.scale);
  draw();
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.image) {
    renderPoints();
    return;
  }
  ctx.drawImage(state.image, 0, 0, canvas.width, canvas.height);
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#1a73e8";
  ctx.fillStyle = "#1a73e8";
  ctx.font = "12px Segoe UI";

  if (state.points.length > 1) {
    ctx.beginPath();
    state.points.forEach((point, index) => {
      const x = point[0] * state.scale;
      const y = point[1] * state.scale;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (state.points.length === 4) ctx.closePath();
    ctx.stroke();
  }

  state.points.forEach((point, index) => {
    const x = point[0] * state.scale;
    const y = point[1] * state.scale;
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.fillText(String(index + 1), x - 3, y + 4);
    ctx.fillStyle = "#1a73e8";
  });
  renderPoints();
}

function renderPoints() {
  pointList.innerHTML = "";
  const labels = ["左上", "右上", "右下", "左下"];
  for (let i = 0; i < 4; i += 1) {
    const li = document.createElement("li");
    const point = state.points[i];
    li.textContent = point
      ? `${labels[i]}：${Math.round(point[0])}, ${Math.round(point[1])}`
      : `${labels[i]}：待选择`;
    pointList.appendChild(li);
  }
  updateDecodeButton();
}

async function upload(file) {
  resetInputsForNewImage();
  clearImageBeforeUpload();
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/upload", { method: "POST", body: form });
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  state.imageId = data.imageId;
  state.naturalWidth = data.width;
  state.naturalHeight = data.height;
  const image = new Image();
  image.onload = () => {
    state.image = image;
    emptyState.classList.add("hidden");
    fitCanvas();
    updateDecodeButton();
  };
  image.src = data.preview;
}

fileInput.addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    await upload(file);
  } catch (error) {
    alert("上传失败，请重新选择图片");
  } finally {
    fileInput.value = "";
  }
});

traceModeInputs.forEach((input) => {
  input.addEventListener("change", () => {
    state.traceMode = input.value;
    state.points = [];
    clearResult();
    draw();
  });
});

canvas.addEventListener("click", (event) => {
  if (!state.image || state.traceMode !== "manual" || state.points.length >= 4) return;
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) / state.scale;
  const y = (event.clientY - rect.top) / state.scale;
  state.points.push([x, y]);
  clearResult();
  draw();
});

undoBtn.addEventListener("click", () => {
  state.points.pop();
  clearResult();
  draw();
});

resetBtn.addEventListener("click", () => {
  state.points = [];
  clearResult();
  draw();
});

decodeBtn.addEventListener("click", async () => {
  decodeBtn.disabled = true;
  decodeBtn.textContent = "正在溯源...";
  const corners = state.traceMode === "manual" && state.points.length === 4 ? state.points : [];
  const response = await fetch("/api/decode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      imageId: state.imageId,
      traceMode: state.traceMode,
      corners,
      cameraMode: true,
      autoTile: true,
      tileSize: Number(tilePx.value || 256),
      tilePx: Number(tilePx.value || 256),
      offsetStep: Number(offsetStep.value || 32),
    }),
  });
  const data = await response.json();
  updateDecodeButton();
  if (!response.ok) {
    alert(data.error || "溯源失败");
    return;
  }
  showResult(data);
});

function showResult(data) {
  if (data.cornerSource === "auto" && Array.isArray(data.corners)) {
    state.points = data.corners;
    draw();
  }
  resultPanel.classList.remove("hidden");
  statusBadge.textContent = data.status === "ok" ? "可信" : "低置信度";
  statusBadge.className = `badge ${data.status === "ok" ? "ok" : "low"}`;
  const cornerLabels = {
    auto: "自动识别",
    manual: "手动选点",
    partial: "部分屏",
    original: "局部原图",
  };
  const cornerLabel = cornerLabels[data.cornerSource] || data.cornerSource || "-";
  const entries = [
    ["记录编号", data.attemptId],
    [data.status === "ok" ? "水印载荷" : "候选载荷", data.payload],
    ["登记对象", data.registry?.subject || "未登记"],
    ["设备", data.registry?.device || "-"],
    ["会话", data.registry?.session || "-"],
    ["选点方式", cornerLabel],
    ["纠错次数", data.correctedCodewords],
    ["图案置信度", data.patternConfidence.toFixed(4)],
    ["比特置信度", data.bitConfidence.toFixed(4)],
    ["同步置信度", data.syncConfidence.toFixed(4)],
    ["使用块数", data.tilesUsed],
    ["实际块尺寸", data.tilePx],
    ["候选块尺寸", data.tileCandidates?.join(", ") || "-"],
    ["最佳偏移", data.bestOffset.join(", ")],
    [["original", "partial"].includes(data.cornerSource) ? "处理尺寸" : "矫正尺寸", data.warpSize.join(" x ")],
  ];
  if (data.diagnosis?.message) {
    entries.push(["结果说明", data.diagnosis.message]);
  }
  if (data.diagnosis?.recommendation) {
    entries.push(["建议", data.diagnosis.recommendation]);
  }
  resultList.innerHTML = "";
  entries.forEach(([key, value]) => {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value;
    resultList.append(dt, dd);
  });
  rectifiedPanel.classList.remove("hidden");
  rectifiedImage.src = data.rectifiedPreview;
  refreshStats();
  refreshHistory();
}

window.addEventListener("resize", fitCanvas);

saveRegistryBtn.addEventListener("click", async () => {
  const payload = payloadInput.value.trim().toLowerCase();
  const subject = subjectInput.value.trim();
  const response = await fetch("/api/registry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      payload,
      subject,
      device: deviceInput.value.trim(),
      session: sessionInput.value.trim(),
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.error || "保存失败");
    return;
  }
  alert("登记已保存");
  await refreshHistory();
});

function percent(value) {
  return `${Math.round(value * 1000) / 10}%`;
}

async function refreshStats() {
  const response = await fetch("/api/stats");
  const data = await response.json();
  successRate.textContent = percent(data.successRate);
  recentRate.textContent = percent(data.recentSuccessRate);
  statsDetail.textContent = data.total
    ? `可信 ${data.ok} 次 / 共 ${data.total} 次，低置信度 ${data.lowConfidence} 次。`
    : "暂无溯源记录。";
}

async function refreshHistory() {
  const response = await fetch("/api/history?limit=30");
  const data = await response.json();
  historyList.innerHTML = "";
  if (!data.items.length) {
    historyList.textContent = "暂无历史记录。";
    return;
  }
  data.items.forEach((item) => {
    const div = document.createElement("div");
    div.className = `history-item ${item.status}`;
    const who = item.subject ? `${item.subject}${item.device ? ` / ${item.device}` : ""}` : "未登记";
    const status = item.status === "ok" ? "可信" : "低置信度";
    div.innerHTML = `
      <strong>${status} · ${item.payload}</strong>
      <span>${who}</span>
      <span>${item.created_at}</span>
      <span>图案置信度 ${Number(item.pattern_confidence).toFixed(3)} · 纠错 ${item.corrected_codewords}</span>
    `;
    historyList.appendChild(div);
  });
}

renderPoints();
refreshStats();
refreshHistory();
