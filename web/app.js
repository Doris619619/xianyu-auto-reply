/**
 * 本文件负责控制面板的短轮询与操作绑定。
 * 属于 web 前端，只调用本地 REST API，不直接操作 Playwright。
 */

const statusLabel = {
  queued: "排队中",
  active: "进行中",
  parked: "暂挂",
  done_agreed: "同意降价",
  done_refused: "明确不降",
  done_manual: "已手动结束",
  failed: "失败",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!response.ok) {
    const detail = data?.detail || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function $(id) {
  return document.getElementById(id);
}

async function refreshQueue() {
  const data = await api("/api/items");
  const running = data.worker_running === true;
  const enabled = data.worker_enabled === true;
  $("worker-status").textContent = running
    ? "状态：运行中（正在处理）"
    : enabled
      ? "状态：开关开着但未真正运行，请再点一次启动"
      : "状态：已停止";
  const box = $("queue-list");
  if (!data.items.length) {
    box.innerHTML = '<p class="empty">队列为空，先贴一个商品链接。</p>';
    return;
  }
  box.innerHTML = data.items
    .map((item) => {
      const rank =
        item.status === "queued" && item.position_rank
          ? ` · 排第 ${item.position_rank}`
          : "";
      const actions = [];
      if (item.status === "queued" || item.status === "parked") {
        actions.push(
          `<button class="small warn" data-act="prioritize" data-id="${item.id}">优先插队</button>`
        );
      }
      if (item.status === "parked") {
        actions.push(
          `<button class="small" data-act="retry" data-id="${item.id}">重试</button>`
        );
      }
      if (["queued", "active", "parked"].includes(item.status)) {
        actions.push(
          `<button class="small danger" data-act="stop" data-id="${item.id}">结束</button>`
        );
      }
      return `<article class="item">
        <div class="item-head">
          <strong>${item.title || item.item_id}</strong>
          <span class="badge">${statusLabel[item.status] || item.status}${rank}</span>
        </div>
        <div class="hint">ID ${item.item_id} · 已发 ${item.rounds_sent} 轮
          ${item.result_summary ? " · " + item.result_summary : ""}</div>
        <div class="item-actions">${actions.join("")}</div>
      </article>`;
    })
    .join("");
}

async function refreshSession() {
  const data = await api("/api/session/current");
  const box = $("session-box");
  if (!data.item) {
    box.innerHTML = '<p class="empty">当前没有锁定会话。</p>';
    return;
  }
  const msgs = data.messages.length
    ? data.messages
        .map(
          (m) => `<div class="msg"><div class="who">${m.speaker === "me" ? "我" : "卖家"}</div><div>${escapeHtml(m.text)}</div></div>`
        )
        .join("")
    : '<p class="empty">还没有消息。</p>';
  box.innerHTML = `<div class="hint" style="margin-bottom:.6rem">商品 ${data.item.item_id} · ${statusLabel[data.item.status]}</div>${msgs}`;
}

async function refreshSettings() {
  const s = await api("/api/settings");
  $("timeout-input").value = s.reply_timeout_seconds;
  $("rounds-input").value = s.max_rounds;
  $("auto-send-input").checked = s.auto_send;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

$("enqueue-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("url-input").value.trim();
  try {
    const res = await api("/api/items", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    $("enqueue-hint").textContent = res.message;
    $("url-input").value = "";
    await refreshQueue();
  } catch (error) {
    $("enqueue-hint").textContent = error.message;
  }
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/api/settings", {
    method: "PATCH",
    body: JSON.stringify({
      reply_timeout_seconds: Number($("timeout-input").value),
      max_rounds: Number($("rounds-input").value),
      auto_send: $("auto-send-input").checked,
    }),
  });
  $("enqueue-hint").textContent = "设置已保存";
});

$("btn-start").addEventListener("click", async () => {
  try {
    const res = await api("/api/worker/start", { method: "POST", body: "{}" });
    $("worker-status").textContent = res.message;
    await refreshQueue();
  } catch (error) {
    $("worker-status").textContent = error.message;
  }
});

$("btn-stop").addEventListener("click", async () => {
  const res = await api("/api/worker/stop", { method: "POST", body: "{}" });
  $("worker-status").textContent = res.message;
  await refreshQueue();
});

$("queue-list").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;
  const path =
    act === "prioritize"
      ? `/api/items/${id}/prioritize`
      : act === "retry"
        ? `/api/items/${id}/retry`
        : `/api/items/${id}/stop`;
  try {
    await api(path, { method: "POST", body: "{}" });
    await refreshQueue();
    await refreshSession();
  } catch (error) {
    $("enqueue-hint").textContent = error.message;
  }
});

async function tick() {
  try {
    await Promise.all([refreshQueue(), refreshSession()]);
  } catch (error) {
    $("worker-status").textContent = `轮询失败：${error.message}`;
  }
}

refreshSettings().catch(() => {});
tick();
setInterval(tick, 2000);
