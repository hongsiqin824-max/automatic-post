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
    $$('[data-quality-recheck]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        if (!window.confirm("重新按当前规则执行一次局部优化和完整质检？只有完整质检明确通过才会进入待发队列。")) return;
        try {
          const result = await postJSON(`/api/articles/${button.dataset.id}/recheck-quality`);
          reloadSoon(result.message || "重新质检已完成");
        } catch (error) { toast(error.message, "error"); }
      }));
    });

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
      form.elements.fallback_litpic.value = values.fallbackLitpic || "";
      $('[data-tab-dialog-title]', dialog).textContent = values.id ? "编辑栏目" : "新增栏目";
      dialog.showModal();
      form.elements.name.focus();
    }

    $('[data-open-tab-dialog]')?.addEventListener("click", () => openTabDialog());
    $$('[data-edit-tab]').forEach((button) => {
      button.addEventListener("click", () => openTabDialog({
        id: button.dataset.id,
        name: button.dataset.name,
        backendId: button.dataset.backendId,
        fallbackLitpic: button.dataset.fallbackLitpic || "",
      }));
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
          fallback_litpic: form.elements.fallback_litpic.value.trim(),
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

    $$('[data-toggle-tab-publish-mode]').forEach((button) => {
      button.addEventListener("click", () => withBusy(button, async () => {
        const currentMode = Number.parseInt(button.dataset.currentMode || "0", 10);
        const newMode = currentMode === 0 ? 1 : 0;
        const modeName = newMode === 1 ? "直接发布" : "创建草稿";
        const warning = newMode === 1
          ? "该栏目尚未首次提交的文章之后会直接上线。"
          : "该栏目尚未首次提交的文章之后会进入草稿箱。";
        if (!window.confirm(`确认切换到「${modeName}」模式吗？\n\n${warning}`)) return;
        try {
          const result = await postJSON(`/api/tabs/${button.dataset.id}`, {
            publish_mode: newMode,
          });
          button.dataset.currentMode = String(result.tab?.publish_mode ?? newMode);
          const row = button.closest("[data-tab-row]");
          const badge = $('[data-tab-publish-mode-badge]', row);
          if (badge) {
            badge.classList.toggle("status-enabled", newMode === 1);
            badge.classList.toggle("status-disabled", newMode !== 1);
            badge.replaceChildren();
            const dot = document.createElement("span");
            badge.append(dot, modeName);
          }
          (result.affected_sources || []).forEach((source) => {
            const sourceRow = $(`[data-source-row][data-code="${CSS.escape(source.code)}"]`);
            if (sourceRow) updateSourceRow(sourceRow, source);
          });
          toast(result.message || `已切换到「${modeName}」模式`);
        } catch (error) { toast(error.message, "error"); }
      }));
    });
  }

  function initCompetitionRules() {
    const list = $('[data-competition-rule-list]');
    const dialog = $("#competition-rule-dialog");
    const form = $('[data-competition-rule-form]');
    if (!list || !dialog || !form) return;

    const search = $('[data-competition-rule-search]');
    const pendingOnly = $('[data-pending-rule-filter]');
    const count = $('[data-competition-rule-count]');
    const filterEmpty = $('[data-competition-rule-filter-empty]');

    function toBeijing(value) {
      if (!value) return "尚无文章";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date).replaceAll("/", "-");
    }

    function normalizedRule(rule) {
      const tab = rule?.tab && typeof rule.tab === "object" ? rule.tab : {};
      const rawPublishMode = rule.publish_mode_override;
      return {
        ...rule,
        id: Number(rule.id),
        marker_type: String(rule.marker_type || "league").toLowerCase(),
        marker_code: String(rule.marker_code || "").toLowerCase(),
        tab_id: rule.tab_id ?? tab.id ?? null,
        tab_name: rule.tab_name || tab.name || "",
        backend_tab_id: rule.backend_tab_id ?? tab.backend_tab_id ?? null,
        source_code: String(rule.source_code || ""),
        source_display_name: String(rule.source_display_name || rule.source_name || ""),
        publish_mode_override: rawPublishMode === 0 || rawPublishMode === 1 ? rawPublishMode : null,
        sample_source: rule.sample_source || "",
        enabled: Boolean(rule.enabled),
      };
    }

    function publishActionLabel(value) {
      if (value === 1) return "直接发布";
      if (value === 0) return "创建草稿";
      return "跟随原配置";
    }

    function sourceScopeLabel(rule) {
      if (!rule.source_code) return "适用全部来源";
      const name = rule.source_display_name || rule.source_code;
      return name === rule.source_code ? `来源：${name}` : `来源：${name} · ${rule.source_code}`;
    }

    function node(tag, className, text) {
      const item = document.createElement(tag);
      if (className) item.className = className;
      if (text !== undefined) item.textContent = text;
      return item;
    }

    function iconButton(hook, icon, title, disabled = false) {
      const button = node("button", "icon-button subtle");
      button.type = "button";
      button.dataset[hook] = "";
      button.title = title;
      button.setAttribute("aria-label", title);
      button.disabled = disabled;
      const iconNode = node("i");
      iconNode.dataset.lucide = icon;
      button.append(iconNode);
      return button;
    }

    function makeRuleRow(rawRule) {
      const rule = normalizedRule(rawRule);
      const pending = !rule.tab_id;
      const row = node("div", "competition-rule-row");
      row.dataset.competitionRuleRow = "";
      row.dataset.id = String(rule.id);
      row.dataset.markerType = rule.marker_type;
      row.dataset.markerCode = rule.marker_code;
      row.dataset.tabId = rule.tab_id == null ? "" : String(rule.tab_id);
      row.dataset.sourceCode = rule.source_code;
      row.dataset.publishModeOverride = rule.publish_mode_override == null
        ? ""
        : String(rule.publish_mode_override);
      row.dataset.enabled = String(rule.enabled);
      row.dataset.pending = String(pending);
      row.dataset.searchText = [
        rule.marker_type, rule.marker_code, rule.tab_name,
        rule.backend_tab_id ?? "", rule.source_code, rule.source_display_name,
        publishActionLabel(rule.publish_mode_override), rule.sample_source,
      ].join(" ").toLowerCase();

      const marker = node("div", "competition-rule-marker");
      marker.append(node("span", `rule-type-badge rule-type-${rule.marker_type}`, rule.marker_type));
      const markerCopy = node("div");
      const strong = node("strong");
      strong.append(node("code", "", rule.marker_code));
      markerCopy.append(strong, node("small", "", sourceScopeLabel(rule)));
      marker.append(markerCopy);

      const target = node("div", "competition-rule-target");
      target.dataset.competitionRuleTarget = "";
      if (pending) {
        target.append(
          node("strong", "pending-text", "待配置"),
          node("small", "", `沿用来源原栏目 · ${publishActionLabel(rule.publish_mode_override)}`),
        );
      } else {
        target.append(
          node("strong", "", rule.tab_name || "栏目已删除"),
          node("small", "", `后台栏目 ID：${rule.backend_tab_id ?? "未知"} · ${publishActionLabel(rule.publish_mode_override)}`),
        );
      }

      const seen = node("div", "competition-rule-seen");
      seen.append(node("span", "", "最近发现"), node("strong", "", toBeijing(rule.last_seen_at)));

      const actions = node("div", "competition-rule-actions");
      const status = node("span", `status-badge ${rule.enabled ? "status-enabled" : "status-disabled"}`);
      status.dataset.competitionRuleStatus = "";
      status.append(node("span"), document.createTextNode(rule.enabled ? "启用" : "停用"));
      const toggleLabel = node("label", "switch");
      toggleLabel.title = rule.enabled ? "停用规则" : "启用规则";
      const toggle = node("input");
      toggle.type = "checkbox";
      toggle.checked = rule.enabled;
      toggle.dataset.competitionRuleEnabled = "";
      toggle.setAttribute("aria-label", `${rule.enabled ? "停用" : "启用"} ${rule.marker_code} 规则`);
      toggleLabel.append(toggle, node("span"));
      actions.append(
        status,
        iconButton("clearCompetitionRule", "unlink", `清空 ${rule.marker_code} 的目标栏目`, pending),
        iconButton("editCompetitionRule", "pencil", `编辑 ${rule.marker_code} 赛事规则`),
        toggleLabel,
      );
      row.append(marker, target, seen, actions);
      return row;
    }

    function applyFilter() {
      const query = String(search?.value || "").trim().toLowerCase();
      const onlyPending = Boolean(pendingOnly?.checked);
      const rows = $$('[data-competition-rule-row]', list);
      let visible = 0;
      rows.forEach((row) => {
        const matches = (!query || (row.dataset.searchText || "").includes(query))
          && (!onlyPending || row.dataset.pending === "true");
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      if (count) {
        count.dataset.total = String(rows.length);
        count.textContent = query || onlyPending ? `${visible} / ${rows.length} 条` : `${rows.length} 条`;
      }
      if (filterEmpty) filterEmpty.hidden = rows.length === 0 || visible > 0;
    }

    function replaceRuleRow(rawRule) {
      const rule = normalizedRule(rawRule);
      const next = makeRuleRow(rule);
      const current = $(`[data-competition-rule-row][data-id="${rule.id}"]`, list);
      if (current) current.replaceWith(next);
      else {
        $('[data-competition-rule-empty]', list)?.remove();
        list.append(next);
      }
      refreshIcons();
      applyFilter();
    }

    function editRule(row) {
      form.reset();
      form.elements.id.value = row.dataset.id;
      form.elements.marker_type.value = row.dataset.markerType;
      form.elements.marker_code.value = row.dataset.markerCode;
      form.elements.tab_id.value = row.dataset.tabId || "";
      form.elements.source_code.value = row.dataset.sourceCode || "";
      form.elements.publish_mode_override.value = row.dataset.publishModeOverride || "";
      form.elements.enabled.checked = row.dataset.enabled === "true";
      $('[data-competition-rule-dialog-title]', dialog).textContent = `编辑 ${row.dataset.markerCode}`;
      dialog.showModal();
      form.elements.tab_id.focus();
    }

    $('[data-open-competition-rule-dialog]')?.addEventListener("click", () => {
      form.reset();
      form.elements.id.value = "";
      form.elements.enabled.checked = true;
      $('[data-competition-rule-dialog-title]', dialog).textContent = "新增规则";
      dialog.showModal();
      form.elements.marker_code.focus();
    });

    search?.addEventListener("input", applyFilter);
    pendingOnly?.addEventListener("change", applyFilter);
    list.addEventListener("click", async (event) => {
      const row = event.target.closest('[data-competition-rule-row]');
      if (!row) return;
      if (event.target.closest('[data-edit-competition-rule]')) {
        editRule(row);
        return;
      }
      const clearButton = event.target.closest('[data-clear-competition-rule]');
      if (!clearButton || clearButton.disabled) return;
      if (!window.confirm("清空目标栏目后，这类文章将沿用来源原栏目。确认清空吗？")) return;
      await withBusy(clearButton, async () => {
        try {
          const result = await postJSON(`/api/event-tab-rules/${row.dataset.id}`, { tab_id: null });
          if (result.event_tab_rule) replaceRuleRow(result.event_tab_rule);
          else reloadSoon(result.message || "目标栏目已清空");
          toast(result.message || "目标栏目已清空");
        } catch (error) { toast(error.message, "error"); }
      });
    });

    list.addEventListener("change", async (event) => {
      const toggle = event.target.closest('[data-competition-rule-enabled]');
      if (!toggle) return;
      const row = toggle.closest('[data-competition-rule-row]');
      const previous = !toggle.checked;
      toggle.disabled = true;
      try {
        const result = await postJSON(`/api/event-tab-rules/${row.dataset.id}`, { enabled: toggle.checked });
        if (result.event_tab_rule) replaceRuleRow(result.event_tab_rule);
        else reloadSoon(result.message || "规则状态已更新");
        toast(result.message || "规则状态已更新");
      } catch (error) {
        toggle.checked = previous;
        toggle.disabled = false;
        toast(error.message, "error");
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      const submitButton = event.submitter;
      await withBusy(submitButton, async () => {
        try {
          const id = form.elements.id.value;
          const publishModeValue = form.elements.publish_mode_override.value;
          const payload = {
            marker_type: form.elements.marker_type.value,
            marker_code: form.elements.marker_code.value.trim().toLowerCase(),
            tab_id: form.elements.tab_id.value ? Number(form.elements.tab_id.value) : null,
            source_code: form.elements.source_code.value || null,
            publish_mode_override: publishModeValue === "" ? null : Number(publishModeValue),
            enabled: form.elements.enabled.checked,
          };
          if (payload.publish_mode_override === 1
              && !window.confirm("确认将这条规则设置为直接发布吗？匹配文章在首次提交后会立即上线。")) return;
          const result = await postJSON(id ? `/api/event-tab-rules/${id}` : "/api/event-tab-rules", payload);
          dialog.close();
          if (result.event_tab_rule) replaceRuleRow(result.event_tab_rule);
          else reloadSoon(result.message || "赛事栏目规则已保存");
          toast(result.message || "赛事栏目规则已保存");
        } catch (error) { toast(error.message, "error"); }
      });
    });

    applyFilter();
  }

  function initPublishAccounts() {
    const dialog = $("#publish-account-dialog");
    const form = $('[data-publish-account-form]');

    function openAccountDialog(values = {}) {
      if (!dialog || !form) return;
      form.reset();
      form.elements.id.value = values.id || "";
      form.elements.dqd_user_id.value = values.userId || "";
      form.elements.user_name.value = values.userName || "";
      form.elements.enabled.checked = values.enabled ?? true;
      $('[data-publish-account-dialog-title]', dialog).textContent = values.id ? "编辑账号" : "添加账号";
      dialog.showModal();
      form.elements.dqd_user_id.focus();
    }

    $('[data-open-publish-account-dialog]')?.addEventListener("click", () => openAccountDialog());
    $$('[data-edit-publish-account]').forEach((button) => {
      button.addEventListener("click", () => openAccountDialog({
        id: button.dataset.id,
        userId: button.dataset.userId,
        userName: button.dataset.userName,
        enabled: button.dataset.enabled === "true",
      }));
    });

    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      const submitButton = event.submitter;
      await withBusy(submitButton, async () => {
        const id = form.elements.id.value;
        const payload = {
          dqd_user_id: Number(form.elements.dqd_user_id.value),
          user_name: form.elements.user_name.value.trim(),
          enabled: Boolean(form.elements.enabled.checked),
        };
        try {
          const result = await postJSON(id ? `/api/publish-accounts/${id}` : "/api/publish-accounts", payload);
          dialog.close();
          reloadSoon(result.message || (id ? "发布账号已更新" : "发布账号已新增"));
        } catch (error) { toast(error.message, "error"); }
      });
    });

    const poolToggle = $('[data-publish-account-pool-toggle]');
    poolToggle?.addEventListener("change", async () => {
      poolToggle.disabled = true;
      try {
        const result = await postJSON("/api/publish-accounts/toggle", { enabled: poolToggle.checked });
        reloadSoon(result.message || (poolToggle.checked ? "发布账号池已启用" : "发布账号池已停用"));
      } catch (error) {
        poolToggle.checked = !poolToggle.checked;
        toast(error.message, "error");
      } finally { poolToggle.disabled = false; }
    });

    $$('[data-publish-account-enabled]').forEach((toggle) => {
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          const result = await postJSON(`/api/publish-accounts/${toggle.dataset.id}`, { enabled: toggle.checked });
          reloadSoon(result.message || (toggle.checked ? "发布账号已启用" : "发布账号已停用"));
        } catch (error) {
          toggle.checked = !toggle.checked;
          toast(error.message, "error");
        } finally { toggle.disabled = false; }
      });
    });
  }

  function sourcePayload(control, tabIds) {
    const row = control.closest("[data-source-row]");
    const enabled = $('[data-source-enabled]', row);
    const payload = { enabled: Boolean(enabled?.checked) };
    if (Array.isArray(tabIds)) payload.tab_ids = tabIds;
    return payload;
  }

  function updateSourceRow(sourceButton, source) {
    if (!sourceButton || !source) return;
    const row = sourceButton.closest("[data-source-row]");
    if (!row) return;

    const tabIds = Array.isArray(source.tab_ids) ? source.tab_ids : [];
    const mappingButton = $('[data-edit-source-tabs]', row);
    if (mappingButton) mappingButton.dataset.tabIds = JSON.stringify(tabIds);

    const summary = $('[data-source-tab-summary]', row);
    const tabs = Array.isArray(source.tabs) ? source.tabs : [];
    if (summary) {
      summary.replaceChildren();
      if (!tabs.length) {
        const empty = document.createElement("span");
        empty.className = "muted";
        empty.textContent = "未配置栏目";
        summary.append(empty);
      } else {
        tabs.forEach((tab) => {
          const chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = tab.name || `栏目 ${tab.id ?? ""}`;
          summary.append(chip);
        });
      }
    }

    const modeSelect = $('[data-source-publish-mode-select]', row);
    const modeLabel = $('[data-source-publish-mode-label]', row);
    const override = source.publish_mode_override == null ? "" : String(source.publish_mode_override);
    if (modeSelect) {
      modeSelect.value = override;
      modeSelect.dataset.currentOverride = override;
    }
    if (modeLabel) {
      modeLabel.classList.toggle("status-enabled", source.publish_mode_effective === 1);
      modeLabel.classList.toggle("status-disabled", source.publish_mode_effective !== 1);
      const dot = modeLabel.querySelector("span");
      modeLabel.textContent = source.publish_mode_label || "跟随栏目";
      if (dot) modeLabel.prepend(dot);
    }
  }

  function initSources() {
    const createDialog = $("#source-dialog");
    const createForm = $('[data-source-form]');
    const createOptionSearch = $('[data-new-source-tab-search]');

    function filterNewSourceTabs() {
      const query = createOptionSearch?.value.trim().toLowerCase() || "";
      $$('[data-search-text]', createForm).forEach((option) => {
        option.hidden = Boolean(query) && !option.dataset.searchText.includes(query);
      });
    }

    $('[data-open-source-dialog]')?.addEventListener("click", () => {
      if (!createDialog || !createForm) return;
      createForm.reset();
      createOptionSearch && (createOptionSearch.value = "");
      filterNewSourceTabs();
      createDialog.showModal();
      createForm.elements.code.focus();
    });

    createOptionSearch?.addEventListener("input", filterNewSourceTabs);
    createForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { createDialog.close(); return; }
      const submitButton = event.submitter;
      const tabIds = $$('input[name="tab_ids"]:checked', createForm).map((input) => Number(input.value));
      if (createForm.elements.publish_mode_override.value === "1"
          && !window.confirm("确认将这个来源设置为强制直接发布吗？该来源尚未首次提交的文章会立即上线。")) return;
      await withBusy(submitButton, async () => {
        try {
          const result = await postJSON("/api/sources", {
            code: createForm.elements.code.value.trim(),
            display_name: createForm.elements.display_name.value.trim(),
            tab_ids: tabIds,
            enabled: Boolean(createForm.elements.enabled.checked),
            publish_mode_override: createForm.elements.publish_mode_override.value === ""
              ? null
              : Number(createForm.elements.publish_mode_override.value),
          });
          createDialog.close();
          reloadSoon(result.message || "来源已新增");
        } catch (error) { toast(error.message, "error"); }
      });
    });

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
    const dialog = $("#source-tabs-dialog");
    const form = $('[data-source-tabs-form]');
    const optionSearch = $('[data-source-tab-search]');
    let activeButton = null;

    function filterSourceTabs() {
      const query = optionSearch?.value.trim().toLowerCase() || "";
      $$('[data-search-text]', form).forEach((option) => {
        option.hidden = Boolean(query) && !option.dataset.searchText.includes(query);
      });
    }

    $$('[data-edit-source-tabs]').forEach((button) => {
      button.addEventListener("click", () => {
        activeButton = button;
        const selected = new Set(JSON.parse(button.dataset.tabIds || "[]").map(Number));
        form.reset();
        form.elements.source_code.value = button.dataset.code;
        $$('input[name="tab_ids"]', form).forEach((input) => {
          input.checked = selected.has(Number(input.value));
        });
        $('[data-source-tabs-title]', dialog).textContent = `${button.dataset.name} · 选择栏目`;
        filterSourceTabs();
        dialog.showModal();
        optionSearch?.focus();
      });
    });
    optionSearch?.addEventListener("input", filterSourceTabs);
    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      const submitButton = event.submitter;
      const tabIds = $$('input[name="tab_ids"]:checked', form).map((input) => Number(input.value));
      await withBusy(submitButton, async () => {
        try {
          const code = form.elements.source_code.value;
          const result = await postJSON(`/api/sources/${encodeURIComponent(code)}`, sourcePayload(activeButton, tabIds));
          dialog.close();
          updateSourceRow(activeButton, result.source);
          toast(result.message || "来源栏目已更新");
        } catch (error) { toast(error.message, "error"); }
      });
    });
    $$('[data-source-enabled]').forEach((toggle) => {
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          const result = await postJSON(`/api/sources/${encodeURIComponent(toggle.dataset.code)}`, sourcePayload(toggle));
          toast(result.message || (toggle.checked ? "来源已启用" : "来源已停用"));
        } catch (error) {
          toggle.checked = !toggle.checked;
          toast(error.message, "error");
        } finally { toggle.disabled = false; }
      });
    });

    $$('[data-source-publish-mode-select]').forEach((select) => {
      select.addEventListener("change", async () => {
        const previous = select.dataset.currentOverride || "";
        const next = select.value;
        if (next === "1"
            && !window.confirm("确认强制这个来源直接发布吗？它将不再跟随栏目模式，尚未首次提交的文章会立即上线。")) {
          select.value = previous;
          return;
        }
        select.disabled = true;
        try {
          const result = await postJSON(`/api/sources/${encodeURIComponent(select.dataset.code)}`, {
            publish_mode_override: next === "" ? null : Number(next),
          });
          updateSourceRow(select, result.source);
          toast(result.message || "来源发布模式已更新");
        } catch (error) {
          select.value = previous;
          toast(error.message, "error");
        } finally { select.disabled = false; }
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
  initCompetitionRules();
  initPublishAccounts();
  initSources();
  });
})();
