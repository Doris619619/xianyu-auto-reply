/**
 * 本文件负责控制面板的短轮询与操作绑定。
 * 属于 web 前端，只调用本地 REST API，不直接操作 Playwright。
 */

const statusLabel = {
  queued: "排队中",
  active: "进行中",
  parked: "暂挂",
  done_agreed: "已谈成",
  done_refused: "未谈成",
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

function sendDiagnosticHint(diagnostic) {
  if (!diagnostic) return "";
  const parts = ["发送诊断：" + diagnostic.phase];
  if (diagnostic.button_center_obscured === true) {
    parts.push("按钮中心被遮挡，未找到其他安全点击区域");
  }
  if (diagnostic.risk_detected_after_click === true) parts.push("点击后检测到风险页");
  if (diagnostic.confirmation_observed === false) parts.push("未确认本人消息回显");
  return `<div class="hint">${escapeHtml(parts.join(" · "))}</div>`;
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
      if (item.status === "failed" && item.rounds_sent > 0) {
        actions.push(
          `<button class="small" data-act="resume-monitoring" data-id="${item.id}">恢复监听</button>`
        );
      }
      if (["queued", "active", "parked"].includes(item.status)) {
        actions.push(
          `<button class="small danger" data-act="stop" data-id="${item.id}">结束</button>`
        );
      }
      actions.push(
        `<button class="small danger" data-act="delete" data-id="${item.id}">彻底删除</button>`
      );
      return `<article class="item">
        <div class="item-head">
          <strong>${item.title || item.item_id}</strong>
          <span class="badge">${statusLabel[item.status] || item.status}${rank}</span>
        </div>
        <div class="hint">ID ${item.item_id} · 已发 ${item.rounds_sent} 轮
          ${item.result_summary ? " · " + item.result_summary : ""}</div>
        ${sendDiagnosticHint(item.send_diagnostic)}
        <div class="item-actions">${actions.join("")}</div>
      </article>`;
    })
    .join("");
}

async function refreshSession() {
  const data = await api("/api/session/current");
  const box = $("session-box");
  const form = $("manual-reply-form");
  const submit = $("manual-reply-submit");
  const browserHint = $("browser-connection-hint");
  browserHint.textContent = data.browser?.message || "";
  if (!data.item) {
    box.innerHTML = '<p class="empty">当前没有锁定会话。</p>';
    form.hidden = true;
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
  const manual = data.item.processing_reply_mode === "manual";
  form.hidden = !manual;
  submit.disabled = !data.manual_send_available;
  $("manual-reply-input").disabled = !data.manual_send_available;
  if (manual && !data.manual_send_available) {
    $("manual-reply-hint").textContent = "正在等待会话就绪或上一条发送确认";
  }
}

async function refreshSettings() {
  const s = await api("/api/settings");
  $("timeout-input").value = s.reply_timeout_seconds;
  $("rounds-input").value = s.max_rounds;
  $("reply-mode-input").value = s.reply_mode;
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
      reply_mode: $("reply-mode-input").value,
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

$("manual-reply-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("manual-reply-input");
  const submit = $("manual-reply-submit");
  const hint = $("manual-reply-hint");
  const text = input.value.trim();
  if (!text) {
    hint.textContent = "请输入要发送的内容";
    return;
  }
  if (!window.confirm("确认把这条内容发送给当前商家吗？")) return;
  submit.disabled = true;
  input.disabled = true;
  hint.textContent = "正在等待聊天页面确认发送…";
  try {
    const result = await api("/api/session/manual-reply", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    input.value = "";
    hint.textContent = `${result.message}（第 ${result.rounds_sent} 轮）`;
  } catch (error) {
    hint.textContent = error.message;
  } finally {
    await Promise.all([refreshQueue(), refreshSession()]);
  }
});

$("btn-clear-queue").addEventListener("click", async () => {
  const confirmed = window.confirm(
    "将先停止 Worker，再永久删除所有队列和会话记录。此操作无法恢复，确定继续吗？"
  );
  if (!confirmed) return;
  try {
    const res = await api("/api/items", { method: "DELETE" });
    $("worker-status").textContent = res.message;
    await Promise.all([refreshQueue(), refreshSession()]);
  } catch (error) {
    $("worker-status").textContent = error.message;
  }
});

$("queue-list").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;
  if (
    act === "delete" &&
    !window.confirm("将永久删除这条记录及其会话内容，无法恢复。确定继续吗？")
  ) {
    return;
  }
  const path =
    act === "prioritize"
      ? `/api/items/${id}/prioritize`
      : act === "retry"
        ? `/api/items/${id}/retry`
        : act === "resume-monitoring"
          ? `/api/items/${id}/resume-monitoring`
        : act === "delete"
          ? `/api/items/${id}`
          : `/api/items/${id}/stop`;
  try {
    await api(path, act === "delete" ? { method: "DELETE" } : { method: "POST", body: "{}" });
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
