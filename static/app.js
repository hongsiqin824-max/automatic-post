(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function refreshIcons() {
    if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }

  function toast(message, type = "success") {
    const region = $("#toast-region");
    if (!region) return;
    const item = document.createElement("div");
    item.className = `toast toast-${type}`;
    const icon = document.createElement("i");
    icon.dataset.lucide = type === "error" ? "circle-alert" : "circle-check";
    const text = document.createElement("span");
    text.textContent = message;
    item.append(icon, text);
    region.append(item);
    refreshIcons();
    window.setTimeout(() => item.remove(), 3800);
  }

  async function postJSON(url, payload = {}) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(payload),
    });
    const raw = await response.text();
    let data = {};
    if (raw) {
      try { data = JSON.parse(raw); } catch { data = { message: raw }; }
    }
    if (!response.ok || data.success === false) {
      throw new Error(data.error || data.message || `请求失败（${response.status}）`);
    }
    return data;
  }

  async function withBusy(button, task) {
    if (!button || button.disabled) return;
    button.disabled = true;
    button.classList.add("is-busy");
    try { return await task(); }
    finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function reloadSoon(message) {
    toast(message);
    window.setTimeout(() => window.location.reload(), 550);
  }

  function initNavigation() {
    $$('[data-sidebar-toggle]').forEach((button) => {
      button.addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") document.body.classList.remove("sidebar-open");
    });
  }

  function initPipelineActions() {
    $$('[data-run-now]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        try {
          const result = await postJSON("/api/run");
          reloadSoon(result.message || "拉取任务已启动");
        } catch (error) { toast(error.message, "error"); }
      }));
    });

    $$('[data-scheduler-toggle]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        const enabled = button.dataset.enabled === "true";
        try {
          const result = await postJSON("/api/scheduler/toggle", { enabled: !enabled });
          reloadSoon(result.message || (!enabled ? "自动任务已恢复" : "自动任务已暂停"));
        } catch (error) { toast(error.message, "error"); }
      }));
    });
  }

  function initOpenPlatformAuth() {
    const startButton = $('[data-open-auth-start]');
    startButton?.addEventListener("click", () => withBusy(startButton, async () => {
      try {
        const result = await postJSON("/api/open/auth/start");
        window.location.href = result.authorize_url;
        toast("已打开开放平台授权页面");
      } catch (error) { toast(error.message, "error"); }
    }));

    const refreshButton = $('[data-open-auth-refresh]');
    refreshButton?.addEventListener("click", () => withBusy(refreshButton, async () => {
      try {
        const result = await postJSON("/api/open/auth/refresh");
        reloadSoon(result.message || "token 已刷新");
      } catch (error) { toast(error.message, "error"); }
    }));

    const resetButton = $('[data-open-auth-reset]');
    resetButton?.addEventListener("click", () => withBusy(resetButton, async () => {
      if (!window.confirm("确定要清空当前开放平台授权吗？")) return;
      try {
        const result = await postJSON("/api/open/auth/reset");
        reloadSoon(result.message || "授权已清空");
      } catch (error) { toast(error.message, "error"); }
    }));
  }

  function initReview() {
    $$('[data-review-action]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        const action = button.dataset.reviewAction;
        const articleId = button.dataset.id;
        let reason = "";
        if (action === "reject") {
          reason = window.prompt("请输入驳回原因：", "内容不符合发布要求") || "";
          if (!reason.trim()) return;
        }
        try {
          const result = await postJSON(`/api/articles/${articleId}/review`, { action, reason, note: reason });
          reloadSoon(result.message || (action === "pass" ? "文章已通过审核" : "文章已驳回"));
        } catch (error) { toast(error.message, "error"); }
      }));
    });

    const dialog = $("#fix-dialog");
    const form = $('[data-fix-form]');
    if (!dialog || !form) return;
    $$('[data-open-fix]').forEach((button) => {
      button.addEventListener("click", () => {
        form.elements.article_id.value = button.dataset.id;
        form.elements.title.value = button.dataset.title || "";
        form.elements.body.value = button.dataset.body || "";
        dialog.showModal();
        form.elements.title.focus();
      });
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      const submitButton = event.submitter;
      await withBusy(submitButton, async () => {
        const articleId = form.elements.article_id.value;
        try {
          const result = await postJSON(`/api/articles/${articleId}/review`, {
            action: "manual_fix_then_pass",
            title: form.elements.title.value.trim(),
            body: form.elements.body.value.trim(),
            body_html: form.elements.body.value.trim(),
          });
          dialog.close();
          reloadSoon(result.message || "修正已保存，文章已通过审核");
        } catch (error) { toast(error.message, "error"); }
      });
    });
  }

  function initDraftRetry() {
    const button = $('[data-create-draft]');
    button?.addEventListener("click", () => withBusy(button, async () => {
      const confirmText = button.dataset.confirm || "确认执行草稿操作吗？";
      if (!window.confirm(confirmText)) return;
      try {
        const result = await postJSON(`/api/articles/${button.dataset.id}/create-draft`);
        reloadSoon(result.message || "草稿已重新创建");
      } catch (error) {
        toast(error.message, "error");
      }
    }));
  }

  function initTabs() {
    const dialog = $("#tab-dialog");
    const form = $('[data-tab-form]');
    if (!dialog || !form) return;

    function openTabDialog(values = {}) {
      form.reset();
      form.elements.id.value = values.id || "";
      form.elements.name.value = values.name || "";
      form.elements.backend_tab_id.value = values.backendId || "";
      $('[data-tab-dialog-title]', dialog).textContent = values.id ? "编辑栏目" : "新增栏目";
      dialog.showModal();
      form.elements.name.focus();
    }

    $('[data-open-tab-dialog]')?.addEventListener("click", () => openTabDialog());
    $$('[data-edit-tab]').forEach((button) => {
      button.addEventListener("click", () => openTabDialog({ id: button.dataset.id, name: button.dataset.name, backendId: button.dataset.backendId }));
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      const submitButton = event.submitter;
      await withBusy(submitButton, async () => {
        const id = form.elements.id.value;
        const backendId = form.elements.backend_tab_id.value.trim();
        const payload = {
          name: form.elements.name.value.trim(),
          backend_tab_id: backendId || null,
        };
        try {
          const result = await postJSON(id ? `/api/tabs/${id}` : "/api/tabs", payload);
          dialog.close();
          reloadSoon(result.message || (id ? "栏目已更新" : "栏目已新增"));
        } catch (error) { toast(error.message, "error"); }
      });
    });

    $$('[data-toggle-tab]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        const enabled = button.dataset.enabled === "true";
        try {
          const result = await postJSON(`/api/tabs/${button.dataset.id}`, { enabled: !enabled });
          reloadSoon(result.message || (!enabled ? "栏目已启用" : "栏目已停用"));
        } catch (error) { toast(error.message, "error"); }
      }));
    });
  }

  function sourcePayload(control) {
    const row = control.closest(".source-row");
    const select = $('[data-source-tab]', row);
    const enabled = $('[data-source-enabled]', row);
    return {
      tab_id: select?.value ? Number(select.value) : null,
      clear_tab: !select?.value,
      enabled: Boolean(enabled?.checked),
    };
  }

  function initSources() {
    const search = $('[data-source-search]');
    search?.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      let visible = 0;
      $$(".source-row").forEach((row) => {
        row.hidden = Boolean(query) && !row.textContent.toLowerCase().includes(query);
        if (!row.hidden) visible += 1;
      });
      const count = $('[data-source-count]');
      if (count) count.textContent = query ? `${visible} / ${count.dataset.total} 个` : `${count.dataset.total} 个`;
    });
    $$('[data-source-tab]').forEach((select) => {
      select.addEventListener("change", async () => {
        select.disabled = true;
        try {
          const result = await postJSON(`/api/sources/${encodeURIComponent(select.dataset.code)}`, sourcePayload(select));
          toast(result.message || "来源栏目已更新");
        } catch (error) { toast(error.message, "error"); }
        finally { select.disabled = false; }
      });
    });
    $$('[data-source-enabled]').forEach((toggle) => {
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        const select = $('[data-source-tab]', toggle.closest(".source-row"));
        try {
          const result = await postJSON(`/api/sources/${encodeURIComponent(toggle.dataset.code)}`, sourcePayload(toggle));
          select.disabled = false;
          toast(result.message || (toggle.checked ? "来源已启用" : "来源已停用"));
        } catch (error) {
          toggle.checked = !toggle.checked;
          toast(error.message, "error");
        } finally { toggle.disabled = false; }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    refreshIcons();
  initNavigation();
  initPipelineActions();
  initOpenPlatformAuth();
  initReview();
  initDraftRetry();
  initTabs();
  initSources();
  });
})();
