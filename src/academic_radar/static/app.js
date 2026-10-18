(() => {
  "use strict";
  const appearanceKey = "daily-literature.appearance.v1";
  const normalizeAppearance = (value) => ({
    font: value?.font === "serif" ? "serif" : "system",
    theme: value?.theme === "paper" ? "paper" : "sage",
  });
  const readAppearance = () => {
    try { return normalizeAppearance(JSON.parse(localStorage.getItem(appearanceKey))); }
    catch (_) { return normalizeAppearance(null); }
  };
  const applyAppearance = (value) => {
    const preferences = normalizeAppearance(value);
    document.documentElement.dataset.font = preferences.font;
    document.documentElement.dataset.theme = preferences.theme;
    return preferences;
  };
  // This self-hosted script runs in the head, before the page is painted.
  applyAppearance(readAppearance());
  const onReady = () => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toast = document.getElementById("toast");
  const announce = (message, error = false) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.hidden = false;
    clearTimeout(announce.timer);
    announce.timer = setTimeout(() => { toast.hidden = true; }, 4200);
  };
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {})},
    });
    const data = await response.json().catch(() => ({error: "服务器返回了无法识别的响应"}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  };

  const appearanceDialog = document.getElementById("appearance-dialog");
  const showAppearance = (value) => {
    const preferences = applyAppearance(value);
    appearanceDialog?.querySelectorAll('input[name="appearance-font"], input[name="appearance-theme"]').forEach((input) => {
      const key = input.name === "appearance-font" ? "font" : "theme";
      input.checked = input.value === preferences[key];
    });
    return preferences;
  };
  const saveAppearance = (value) => {
    const preferences = showAppearance(value);
    const status = appearanceDialog?.querySelector("[data-appearance-status]");
    try {
      localStorage.setItem(appearanceKey, JSON.stringify(preferences));
      if (status) status.textContent = "已保存到此浏览器";
    } catch (_) {
      if (status) status.textContent = "已应用；浏览器暂时无法保存偏好";
    }
  };
  showAppearance(readAppearance());
  document.querySelector("[data-open-appearance]")?.addEventListener("click", () => appearanceDialog?.showModal());
  appearanceDialog?.addEventListener("change", (event) => {
    if (!event.target.matches('input[name="appearance-font"], input[name="appearance-theme"]')) return;
    saveAppearance({font: appearanceDialog.querySelector('input[name="appearance-font"]:checked')?.value,
                    theme: appearanceDialog.querySelector('input[name="appearance-theme"]:checked')?.value});
  });
  document.querySelector("[data-reset-appearance]")?.addEventListener("click", () => saveAppearance(null));
  window.addEventListener("storage", (event) => { if (event.key === appearanceKey) showAppearance(readAppearance()); });

  document.querySelectorAll("[data-sync-now]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    const label = document.querySelector("[data-sync-message]");
    try {
      const result = await api("/api/sync", {method: "POST", body: "{}"});
      if (label) label.textContent = result.message;
      announce(result.message);
      let attempts = 0;
      const poll = async () => {
        try {
          const status = await api("/api/sync/status");
          if (label) label.textContent = status.message;
          if (status.pending && ++attempts < 100) setTimeout(poll, 3500);
        } catch (_) { /* Saved requests remain queued during a network interruption. */ }
      };
      setTimeout(poll, 2000);
    } catch (error) { announce(error.message, true); }
    finally { button.disabled = false; }
  }));

  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => {
    button.closest("dialog")?.close();
  }));

  const manualDialog = document.getElementById("manual-paper-dialog");
  document.querySelector("[data-open-manual-paper]")?.addEventListener("click", () => manualDialog?.showModal());
  document.querySelector("[data-manual-paper-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    submit.disabled = true;
    try {
      const result = await api("/api/papers/manual", {method: "POST", body: JSON.stringify({
        apa_citation: values.get("apa_citation"), abstract: values.get("abstract"),
      })});
      form.reset(); manualDialog?.close(); announce(result.message);
    } catch (error) { announce(error.message, true); }
    finally { submit.disabled = false; }
  });

  document.querySelectorAll(".paper-details").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    details.addEventListener("toggle", () => {
      if (summary) {
        const label = summary.querySelector("span");
        if (label) label.textContent = details.open ? "收起" : "详情";
        else summary.textContent = details.open ? "收起详情" : "展开详情";
      }
    });
  });

  document.querySelectorAll("[data-manual-abstract-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    submit.disabled = true;
    try {
      const result = await api("/api/papers/abstract", {method: "POST", body: JSON.stringify({
        identity: values.get("identity"), abstract: values.get("abstract"), source_url: values.get("source_url"),
      })});
      announce(result.message);
      location.reload();
    } catch (error) { announce(error.message, true); }
    finally { submit.disabled = false; }
  }));

  const matchingCards = (identity) => [...document.querySelectorAll("[data-paper-identity]")]
    .filter((card) => card.dataset.paperIdentity === identity);
  const syncFavorite = (card, favorite) => {
    const button = card.querySelector("[data-favorite]");
    if (button) {
      button.setAttribute("aria-pressed", String(favorite));
      button.classList.toggle("is-favorite", favorite);
      const label = button.querySelector("[data-favorite-label]");
      if (label) label.textContent = favorite ? "已收藏" : "收藏";
      button.setAttribute("aria-label", favorite ? "取消收藏" : "收藏文献");
    }
    const checkbox = card.querySelector('input[name="favorite"]');
    if (checkbox) checkbox.checked = favorite;
    const star = card.querySelector(".feedback-heading .star");
    if (star) star.hidden = !favorite;
  };
  const syncReading = (card, readingStatus) => {
    card.dataset.readingStatus = readingStatus;
    const button = card.querySelector("[data-read-later]");
    const later = readingStatus === "read_later";
    if (button) {
      button.setAttribute("aria-pressed", String(later));
      button.setAttribute("aria-label", later ? "移出稍后读" : "加入稍后读");
      button.classList.toggle("is-read-later", later);
      const label = button.querySelector("[data-reading-label]");
      if (label) label.textContent = later ? "已加入稍后读" : "稍后读";
    }
    const select = card.querySelector('select[name="reading_status"]');
    if (select) select.value = readingStatus;
  };
  const setCardBusy = (identity, busy) => matchingCards(identity).forEach((card) => {
    card.querySelectorAll('[data-favorite], [data-read-later], [data-feedback-form] button[type="submit"]').forEach((button) => { button.disabled = busy; });
  });

  const orderRecommendationCards = (list) => {
    if (!list) return;
    const cards = [...list.querySelectorAll(":scope > .paper-card")];
    const pending = cards.filter((card) => !card.dataset.interest);
    const interested = cards.filter((card) => card.dataset.interest === "interested");
    const notInterested = cards.filter((card) => card.dataset.interest === "not_interested");
    [...pending, ...interested, ...notInterested].forEach((card) => list.append(card));
  };

  document.querySelectorAll("[data-feedback-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    const payload = {
      identity: values.get("identity"),
      interest: values.get("interest"),
      reason: values.get("reason"),
      reading_status: values.get("reading_status"),
      favorite: values.get("favorite") === "on",
    };
    setCardBusy(payload.identity, true);
    try {
      const result = await api("/api/feedback", {method: "POST", body: JSON.stringify(payload)});
      matchingCards(payload.identity).forEach((card) => {
        card.dataset.interest = result.interest || "";
        card.dataset.feedbackReason = payload.reason || "";
        const status = card.querySelector("[data-feedback-status]");
        if (status) {
          status.textContent = result.status_label;
          status.hidden = !result.status_label;
          status.className = `completion-status ${result.interest || "neutral"}`;
        }
        const otherForm = card.querySelector("[data-feedback-form]");
        if (otherForm) {
          otherForm.elements.interest.value = result.interest || "";
          otherForm.elements.reason.value = payload.reason;
          otherForm.elements.reading_status.value = payload.reading_status;
        }
        syncFavorite(card, payload.favorite);
        syncReading(card, result.reading_status);
        const list = card.closest("[data-today-list], [data-history-list]");
        if (list) {
          card.classList.add("is-completing");
          orderRecommendationCards(list);
          setTimeout(() => card.classList.remove("is-completing"), 780);
        }
      });
      announce(result.message);
    } catch (error) { announce(error.message, true); }
    finally { setCardBusy(payload.identity, false); }
  }));

  document.addEventListener("click", async (event) => {
    const feedback = event.target.closest("[data-open-feedback]");
    if (feedback) {
      const card = feedback.closest("[data-paper-identity]");
      const details = card?.querySelector(".paper-details");
      if (details) details.open = true;
      requestAnimationFrame(() => card?.querySelector('[data-feedback-form] select[name="interest"]')?.focus());
      return;
    }
    const reading = event.target.closest("[data-read-later]");
    if (reading) {
      const card = reading.closest("[data-paper-identity]");
      const identity = reading.dataset.identity;
      const next = card.dataset.readingStatus === "read_later" ? "unread" : "read_later";
      setCardBusy(identity, true);
      try {
        const result = await api("/api/feedback", {method: "POST", body: JSON.stringify({
          identity, interest: card.dataset.interest || null, reason: card.dataset.feedbackReason || "",
          favorite: card.querySelector("[data-favorite]")?.getAttribute("aria-pressed") === "true",
          reading_status: next,
        })});
        matchingCards(identity).forEach((other) => syncReading(other, result.reading_status));
        announce(next === "read_later" ? "已加入稍后读" : "已移出稍后读");
      } catch (error) { announce(error.message, true); }
      finally { setCardBusy(identity, false); }
      return;
    }
    const favorite = event.target.closest("[data-favorite]");
    if (favorite) {
      event.preventDefault();
      const next = favorite.getAttribute("aria-pressed") !== "true";
      setCardBusy(favorite.dataset.identity, true);
      try {
        const result = await api("/api/favorite", {method: "POST", body: JSON.stringify({identity: favorite.dataset.identity, favorite: next})});
        matchingCards(favorite.dataset.identity).forEach((card) => syncFavorite(card, result.favorite));
        announce(result.message);
      } catch (error) { announce(error.message, true); }
      finally { setCardBusy(favorite.dataset.identity, false); }
    }
  });

  document.querySelectorAll("[data-inline-pdf-form]").forEach((form) => {
    const trigger = form.querySelector("[data-inline-pdf-trigger]");
    const input = form.querySelector("[data-inline-pdf-file]");
    const name = form.querySelector("[data-inline-pdf-name]");
    const submit = form.querySelector("[data-inline-pdf-submit]");
    trigger?.addEventListener("click", () => input?.click());
    input?.addEventListener("change", () => {
      const file = input.files?.[0];
      if (!file || !name || !submit) return;
      name.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
      name.hidden = false;
      submit.hidden = false;
      submit.textContent = form.dataset.hasFulltext === "true" ? "更新上传" : "确认上传";
      trigger.textContent = "重新选择 PDF";
    });
  });

  const sourceRoot = document.querySelector("[data-source-search]");
  if (sourceRoot) {
    const input = sourceRoot.querySelector("#source-query");
    const list = sourceRoot.querySelector("[data-source-status]")?.nextElementSibling;
    const status = sourceRoot.querySelector("[data-source-status]");
    const spinner = sourceRoot.querySelector("[data-source-spinner]");
    let timer = 0, active = -1, controller = null, previewToken = "";
    const options = () => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
    const selectIndex = (index) => {
      const items = options();
      active = items.length ? (index + items.length) % items.length : -1;
      items.forEach((item, i) => {
        item.classList.toggle("active", i === active);
        item.setAttribute("aria-selected", String(i === active));
      });
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", items[active].id);
        items[active].scrollIntoView({block: "nearest"});
      } else input.removeAttribute("aria-activedescendant");
    };
    const preview = async (id) => {
      status.textContent = "正在读取真实作品预览…";
      try {
        const result = await api("/api/sources/preview", {method: "POST", body: JSON.stringify({candidate_id: id})});
        previewToken = result.token;
        const dialog = document.getElementById("source-preview-dialog");
        dialog.querySelector("[data-preview-title]").textContent = `确认添加“${result.source.name}”`;
        const official = result.source.official_status === "verified"
          ? `官网卷期将由 ${result.source.official_provider} 核验最近两期。`
          : "目前先使用 14 天 API 采集；系统会提示后续补充官网适配。";
        dialog.querySelector("[data-preview-summary]").textContent = `从 ${result.since} 起找到 ${result.total_results} 个结果。${official}`;
        const samples = dialog.querySelector("[data-preview-list]");
        samples.replaceChildren(...(result.samples.length ? result.samples : [{title: "当前时间窗没有样例", venue: "可稍后重试"}]).map((item) => {
          const li = document.createElement("li");
          const strong = document.createElement("strong"); strong.textContent = item.title;
          const span = document.createElement("span"); span.textContent = [item.venue, item.doi].filter(Boolean).join(" · ");
          li.append(strong, span); return li;
        }));
        dialog.showModal(); status.textContent = "预览已就绪，确认后才会保存。";
      } catch (error) { status.textContent = error.message; announce(error.message, true); }
    };
    const render = (items, message) => {
      list.replaceChildren();
      items.forEach((item, index) => {
        const button = document.createElement("button");
        button.type = "button"; button.role = "option"; button.id = `source-option-${index}`;
        button.dataset.id = item.candidate_id; button.setAttribute("aria-selected", "false");
        button.setAttribute("aria-disabled", String(Boolean(item.added))); button.disabled = Boolean(item.added);
        const title = document.createElement("strong"); title.textContent = item.name + (item.added ? "（已添加）" : "");
        const meta = document.createElement("span");
        const official = item.official_status === "verified" ? `官网：${item.official_provider}` : "官网适配：待补充";
        meta.textContent = [item.source_type, item.publisher, item.issn && `ISSN ${item.issn}`, item.openalex_id && `OpenAlex ${item.openalex_id}`, official, item.match_basis].filter(Boolean).join(" · ");
        button.append(title, meta); button.addEventListener("click", () => preview(item.candidate_id)); list.append(button);
      });
      list.hidden = items.length === 0; input.setAttribute("aria-expanded", String(items.length > 0));
      status.textContent = message || (items.length ? `找到 ${items.length} 个候选` : "没有找到可验证的来源");
      active = -1; input.removeAttribute("aria-activedescendant");
    };
    input.addEventListener("input", () => {
      clearTimeout(timer); controller?.abort();
      const query = input.value.trim();
      if (query.length < 2) { render([], "请输入至少 2 个字符"); return; }
      spinner.hidden = false; status.textContent = "搜索中…";
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch(`/api/sources/search?q=${encodeURIComponent(query)}`, {signal: controller.signal});
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "搜索失败");
          render(data.items || [], data.message);
        } catch (error) {
          if (error.name !== "AbortError") render([], `${error.message}。请稍后重试。`);
        } finally { spinner.hidden = true; }
      }, 320);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); selectIndex(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); selectIndex(active - 1); }
      else if (event.key === "Enter" && active >= 0) { event.preventDefault(); options()[active]?.click(); }
      else if (event.key === "Escape") {
        list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
      }
    });
    document.querySelector("[data-confirm-source]")?.addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/sources/confirm", {method: "POST", body: JSON.stringify({token: previewToken})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); event.target.disabled = false; }
    });
    document.querySelectorAll("[data-remove-source]").forEach((button) => button.addEventListener("click", async () => {
      const accepted = confirm(`移除“${button.dataset.name}”将停止未来监测。历史论文默认保留，也不会删除收藏、反馈或 PDF。继续吗？`);
      if (!accepted) return;
      button.disabled = true;
      try {
        const result = await api("/api/sources/remove", {method: "POST", body: JSON.stringify({name: button.dataset.name})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); button.disabled = false; }
    }));
  }

  document.querySelector("[data-copy-update]")?.addEventListener("click", async () => {
    const text = document.querySelector("[data-copy-target]")?.value || "";
    try { await navigator.clipboard.writeText(text); announce("更新任务已复制，发送给 Codex 即可执行"); }
    catch (_) { announce("无法访问剪贴板，请手动复制", true); }
  });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady, {once: true});
  else onReady();
})();
