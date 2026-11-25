const $ = (id) => document.getElementById(id);
const sessionFromUrl = new URLSearchParams(location.search).get("session") || "";
if (sessionFromUrl) localStorage.setItem("voiceGachaSession", sessionFromUrl);
let session = sessionFromUrl || localStorage.getItem("voiceGachaSession") || "";
if (sessionFromUrl) {
  const cleanUrl = new URL(location.href);
  cleanUrl.searchParams.delete("session");
  history.replaceState(null, "", `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
}
let selectedType = "girls";
let profile = null;
let spinning = false;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || "通信に失敗しました");
  return data;
}

function setStatus(message) { $("status").textContent = message; }
function updateDisplayedRates() {
  const rates = profile?.gacha_rates?.[selectedType] || profile?.rates;
  if (!rates) return;
  $("rRate").textContent = `${rates.R}%`;
  $("ssrRate").textContent = `${rates.SSR}%`;
}
function setCredits(value) {
  const credits = Math.max(0, Number(value || 0));
  $("credits").textContent = credits.toLocaleString();
  $("spinCredits").textContent = credits.toLocaleString();
  if (profile) profile.credits = credits;
  $("spinButton").disabled = !session || spinning || credits < 1;
}

function errorText(error) {
  const map = {
    "Invalid session": "ログイン情報が無効です。Discordから入り直してください。",
    "No spin credits": "ガチャ回数がありません。シリアルコードを登録してください。",
    "No active prizes": "このガチャには景品がまだ登録されていません。",
    "Invalid serial code": "シリアルコードが見つかりません。",
    "Serial code already redeemed": "このシリアルコードは使用済みです。",
    "Too many serial attempts": "入力回数が多すぎます。5分後にお試しください。"
  };
  return map[error.message] || error.message;
}

async function initialize() {
  if (!session) {
    $("loginNotice").classList.remove("hidden");
    $("loginNotice").textContent = "最初のシリアルコードを入力すると、あなた専用のコレクションが作成されます。使用したコードはコレクション復元にも使えます。";
    $("userName").textContent = "はじめての方";
    setStatus("シリアルコードを登録して始めてください。");
    setCredits(0);
    return;
  }
  try {
    profile = await api(`/api/me?session=${encodeURIComponent(session)}`);
    $("userName").textContent = profile.user_name;
    updateDisplayedRates();
    setCredits(profile.credits);
    setStatus(profile.credits > 0 ? "ガチャを選んで回してください。" : "シリアルコードを登録してください。");
    await loadCollection();
  } catch (error) {
    $("loginNotice").classList.remove("hidden");
    $("loginNotice").textContent = errorText(error);
    $("userName").textContent = "ログインエラー";
    setStatus(errorText(error));
    setCredits(0);
  }
}

document.querySelectorAll(".machine-tab").forEach((button) => {
  button.addEventListener("click", () => {
    selectedType = button.dataset.type;
    document.querySelectorAll(".machine-tab").forEach((item) => item.classList.toggle("active", item === button));
    $("gameStage").dataset.theme = selectedType;
    const girls = selectedType === "girls";
    $("machineImage").src = girls ? "/assets/girls-machine.png" : "/assets/boys-machine.png";
    $("machineImage").alt = girls ? "おんなのこガチャ筐体" : "おとこのこガチャ筐体";
    $("machineKicker").textContent = girls ? "GIRLS COLLECTION" : "BOYS COLLECTION";
    $("machineTitle").textContent = girls ? "おんなのこガチャ" : "おとこのこガチャ";
    updateDisplayedRates();
  });
});

$("serialForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = $("serialCode").value.trim();
  if (!code) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const data = await api("/api/redeem", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session, code})
    });
    if (data.session) {
      session = data.session;
      localStorage.setItem("voiceGachaSession", session);
      profile = await api(`/api/me?session=${encodeURIComponent(session)}`);
      $("userName").textContent = profile.user_name;
      updateDisplayedRates();
      $("loginNotice").classList.add("hidden");
      await loadCollection();
    }
    setCredits(data.credits);
    $("serialCode").value = "";
    setStatus(data.restored ? "コレクションを復元しました。" : data.already_redeemed ? "このコードは登録済みです。" : `${data.added}回分を追加しました！`);
  } catch (error) { setStatus(errorText(error)); }
  finally { button.disabled = false; }
});

$("spinButton").addEventListener("click", async () => {
  if (spinning || !profile?.credits) return;
  spinning = true;
  $("spinButton").disabled = true;
  $("gameStage").classList.add("spinning");
  setStatus("カプセルを抽選中…");
  try {
    const data = await api("/api/spin", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session, gacha_type: selectedType, request_id: crypto.randomUUID()})
    });
    setCredits(data.credits);
    await new Promise((resolve) => setTimeout(resolve, 1100));
    await showPrize(data.prize);
    await loadCollection();
    setStatus(`${data.prize.rarity}「${data.prize.title}」を獲得しました！`);
  } catch (error) { setStatus(errorText(error)); }
  finally {
    spinning = false;
    $("gameStage").classList.remove("spinning");
    setCredits(profile?.credits || 0);
  }
});

async function showPrize(prize) {
  const sound = $("revealSound");
  sound.currentTime = 0;
  sound.volume = .72;
  sound.play().catch(() => {});
  $("rarityBurst").textContent = prize.rarity;
  $("rarityBurst").className = `rarity-burst ${prize.rarity.toLowerCase()}`;
  $("newLabel").classList.toggle("hidden", !prize.is_new);
  $("resultTitle").textContent = prize.title;
  $("resultVideo").src = prize.video_url;
  $("downloadResult").href = prize.download_url;
  $("downloadResult").setAttribute("download", "");
  $("resultDialog").showModal();
  try { await $("resultVideo").play(); } catch {}
}

function closeDialog() {
  $("resultVideo").pause();
  $("resultVideo").removeAttribute("src");
  $("resultVideo").load();
  $("resultDialog").close();
}
$("closeResult").addEventListener("click", closeDialog);
$("closeResultBottom").addEventListener("click", closeDialog);

async function loadCollection() {
  if (!session) return;
  const filter = $("collectionFilter").value;
  const query = filter ? `&gacha_type=${filter}` : "";
  try {
    const data = await api(`/api/collection?session=${encodeURIComponent(session)}${query}`);
    const grid = $("collectionGrid");
    grid.innerHTML = "";
    if (!data.items.length) {
      grid.innerHTML = '<p class="empty">まだボイス動画を持っていません。</p>';
      return;
    }
    data.items.forEach((item) => {
      const card = document.createElement("article");
      card.className = `collection-card ${item.rarity.toLowerCase()}`;
      card.innerHTML = `<span class="rarity ${item.rarity.toLowerCase()}">${item.rarity}</span><h3></h3><p></p><div class="card-actions"><button type="button">再生</button><a download>保存</a></div>`;
      card.querySelector("h3").textContent = item.title;
      card.querySelector("p").textContent = `${item.gacha_type === "girls" ? "おんなのこ" : "おとこのこ"} · ${item.owned_count}回獲得`;
      card.querySelector("button").addEventListener("click", () => showPrize(item));
      const link = card.querySelector("a"); link.href = item.download_url;
      grid.append(card);
    });
  } catch (error) { $("collectionGrid").innerHTML = `<p class="empty">${errorText(error)}</p>`; }
}
$("collectionFilter").addEventListener("change", loadCollection);
$("refreshCollection").addEventListener("click", loadCollection);
initialize();
