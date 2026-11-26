const $ = (id) => document.getElementById(id);
let password = "";

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

const fmt = (value) => value ? new Date(value).toLocaleString("ja-JP") : "—";

function showRates(rates) {
  document.querySelectorAll(".rate-form").forEach((form) => {
    const rate = rates?.[form.dataset.type];
    if (!rate) return;
    form.querySelector(".r-rate").value = rate.R;
    form.querySelector(".ssr-rate").value = rate.SSR;
  });
}

async function load() {
  const data = await api("/api/admin/overview", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password})});
  showRates(data.rates);
  $("stats").innerHTML = `<div class="stat"><small>総抽選回数</small><strong>${data.stats.pulls}</strong></div><div class="stat"><small>コレクション解放</small><strong>${data.stats.unique_unlocks}</strong></div><div class="stat"><small>使用済みコード</small><strong>${data.stats.redeemed_codes}</strong></div><div class="stat"><small>SSR確率</small><strong>女 ${data.rates.girls.SSR}% / 男 ${data.rates.boys.SSR}%</strong></div>`;
  $("prizeList").innerHTML = data.prizes.length ? data.prizes.map((prize) => `<article class="list-item"><span class="badge ${prize.rarity.toLowerCase()}">${prize.rarity}</span><div><strong>${esc(prize.title)}</strong><div class="muted">${prize.gacha_type === "girls" ? "おんなのこ" : "おとこのこ"} · ${esc(prize.original_filename)} · ${fmt(prize.created_at)}</div></div><div class="item-actions"><button class="${prize.active ? "danger" : "secondary"} toggle-prize" data-id="${prize.id}" data-active="${!prize.active}">${prize.active ? "排出停止" : "排出再開"}</button><button class="delete-prize" data-id="${prize.id}" data-title="${esc(prize.title)}">削除</button></div></article>`).join("") : "<p>景品が未登録です。</p>";

  $("prizeList").querySelectorAll(".toggle-prize").forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await api("/api/admin/prizes/toggle", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password, prize_id: Number(button.dataset.id), active: button.dataset.active === "true"})});
        await load();
      } catch (error) { alert(error.message); button.disabled = false; }
    };
  });
  $("prizeList").querySelectorAll(".delete-prize").forEach((button) => {
    button.onclick = async () => {
      if (!confirm(`「${button.dataset.title}」を動画ファイルごと削除しますか？`)) return;
      button.disabled = true;
      try {
        await api("/api/admin/prizes/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password, prize_id: Number(button.dataset.id)})});
        await load();
      } catch (error) { alert(error.message); button.disabled = false; }
    };
  });
  $("codeList").innerHTML = `<table><thead><tr><th>コード末尾</th><th>回数</th><th>メモ</th><th>発行</th><th>状態</th></tr></thead><tbody>${data.codes.map((code) => `<tr><td>${esc(code.code_hint)}</td><td>${code.spins}</td><td>${esc(code.memo || "—")}</td><td>${fmt(code.created_at)}</td><td class="${code.redeemed_at ? "ok" : "unused"}">${code.redeemed_at ? `使用済 ${fmt(code.redeemed_at)}` : "未使用"}</td></tr>`).join("")}</tbody></table>`;
}

$("loginForm").onsubmit = async (event) => {
  event.preventDefault();
  password = $("password").value;
  $("loginStatus").textContent = "確認中…";
  try {
    await api("/api/admin/auth", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password})});
    $("loginCard").classList.add("hidden");
    $("dashboard").classList.remove("hidden");
    await load();
  } catch (error) { password = ""; $("loginStatus").textContent = error.message; }
};

$("codeForm").onsubmit = async (event) => {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const button = event.submitter;
  button.disabled = true;
  try {
    const data = await api("/api/admin/codes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password, count: 1, spins: Number(formData.get("spins")), memo: formData.get("memo")})});
    $("issuedCodes").value = data.codes.join("\n");
    $("issuedBox").classList.remove("hidden");
    await load();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
};

$("copyCodes").onclick = async () => {
  await navigator.clipboard.writeText($("issuedCodes").value);
  $("copyCodes").textContent = "コピー済み";
  setTimeout(() => { $("copyCodes").textContent = "すべてコピー"; }, 1200);
};

$("prizeForm").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const button = event.submitter;
  formData.set("password", password);
  button.disabled = true;
  $("uploadStatus").textContent = "アップロード中…";
  try {
    await api("/api/admin/prizes", {method: "POST", body: formData});
    form.reset();
    $("uploadStatus").textContent = "登録しました。";
    await load();
  } catch (error) { $("uploadStatus").textContent = error.message; }
  finally { button.disabled = false; }
};

document.querySelectorAll(".rate-form").forEach((form) => {
  const rInput = form.querySelector(".r-rate");
  const ssrInput = form.querySelector(".ssr-rate");
  const complement = (source, target) => {
    const value = Number(source.value);
    if (Number.isFinite(value) && value >= 0 && value <= 100) target.value = Number((100 - value).toFixed(2));
  };
  rInput.addEventListener("input", () => complement(rInput, ssrInput));
  ssrInput.addEventListener("input", () => complement(ssrInput, rInput));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    $("rateStatus").textContent = "保存中…";
    try {
      await api("/api/admin/rates", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password, gacha_type: form.dataset.type, r_percent: Number(rInput.value), ssr_percent: Number(ssrInput.value)})});
      $("rateStatus").textContent = `${form.dataset.type === "girls" ? "おんなのこ" : "おとこのこ"}ガチャの排出率を保存しました。`;
      await load();
    } catch (error) { $("rateStatus").textContent = error.message; }
    finally { button.disabled = false; }
  });
});

$("refresh").onclick = load;
