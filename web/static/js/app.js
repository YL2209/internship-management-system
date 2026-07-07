/**
 * ============================================================
 *  实习数据管理系统 Web — 前端共享工具库（性能优化版）
 *  覆盖: API 请求 / 分页 / 模态框 / 表单校验 / 渲染 / A11y
 *  加载: <script src="/static/js/app.js"></script>
 * ============================================================
 */

;(function () {
  'use strict';

  /* ============================
   *  1. HTML 安全转义
   * ============================ */
  const escapeHtml = (function () {
    const div = document.createElement('div');
    return function (str) {
      if (str == null) return '';
      div.textContent = str;
      return div.innerHTML;
    };
  })();

  /* ============================
   *  2. API 客户端
   * ============================ */
  async function api(url, opts) {
    opts = opts || {};
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    let body = opts.body;
    if (body && typeof body === 'object' && !(body instanceof FormData)) {
      body = JSON.stringify(body);
    }

    let resp;
    try {
      resp = await fetch(url, { method: opts.method || 'GET', headers: headers, body: body, credentials: 'same-origin' });
    } catch (e) {
      throw new Error('网络请求失败，请检查连接');
    }

    if (resp.status === 401) {
      if (window.location.pathname !== '/login' && window.location.pathname !== '/') {
        window.location.href = '/login';
      }
      throw new Error('会话已过期，请重新登录');
    }

    let data;
    try {
      data = await resp.json();
    } catch (e) {
      throw new Error('服务器返回格式异常 (HTTP ' + resp.status + ')');
    }

    return data;
  }

  /* ============================
   *  3. 分页组件 (事件委托版本)
   * ============================ */
  function renderPagination(p, containerId, infoId, onPageChange) {
    var infoEl = document.getElementById(infoId);
    var navEl = document.getElementById(containerId);
    if (!navEl) return;

    if (infoEl) {
      infoEl.textContent = '第 ' + p.page + ' 页 / 共 ' + p.total_pages + ' 页';
    }

    if (p.total_pages <= 1) {
      navEl.innerHTML = '';
      return;
    }

    var pages = [];
    var start = Math.max(1, p.page - 2);
    var end = Math.min(p.total_pages, p.page + 2);
    if (start > 1) pages.push(1, '...');
    for (var i = start; i <= end; i++) pages.push(i);
    if (end < p.total_pages) pages.push('...', p.total_pages);

    var html = '';
    html += '<button class="btn btn-sm btn-outline"' + (p.page <= 1 ? ' disabled' : '') +
            ' data-page="' + (p.page - 1) + '">\u2039 上一页</button>';

    for (var j = 0; j < pages.length; j++) {
      var pg = pages[j];
      if (pg === '...') {
        html += '<span class="text-muted px-2">...</span>';
      } else if (pg === p.page) {
        html += '<button class="btn btn-sm btn-primary" data-page="' + pg + '">' + pg + '</button>';
      } else {
        html += '<button class="btn btn-sm btn-outline" data-page="' + pg + '">' + pg + '</button>';
      }
    }

    html += '<button class="btn btn-sm btn-outline"' + (p.page >= p.total_pages ? ' disabled' : '') +
            ' data-page="' + (p.page + 1) + '">下一页 \u203a</button>';

    navEl.innerHTML = html;

    // 使用事件委托，绑定在容器上
    navEl.onclick = function (e) {
      var btn = e.target.closest('[data-page]');
      if (!btn) return;
      var next = parseInt(btn.getAttribute('data-page'), 10);
      if (!isNaN(next) && typeof onPageChange === 'function') {
        onPageChange(next);
      }
    };
  }

  /* ============================
   *  4. 模态框管理（优化滚动锁定）
   * ============================ */
  let activeModals = 0;           // 当前打开的模态框数量
  const bodyScrollY = { value: 0 };

  function modalOpen(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('hidden');
    if (activeModals === 0) {
      // 记录当前滚动位置并锁定
      bodyScrollY.value = window.scrollY;
      document.body.style.overflow = 'hidden';
      document.body.style.position = 'fixed';
      document.body.style.width = '100%';
      document.body.style.top = -bodyScrollY.value + 'px';
    }
    activeModals++;
    el.dataset.scrollY = bodyScrollY.value;
    // 聚焦第一个可聚焦元素
    requestAnimationFrame(() => {
      var focusable = el.querySelector('input, button, select, textarea, [tabindex]:not([tabindex="-1"])');
      if (focusable) focusable.focus();
    });
  }

  function modalClose(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.add('hidden');
    activeModals = Math.max(0, activeModals - 1);
    if (activeModals === 0) {
      // 恢复 body 滚动
      document.body.style.overflow = '';
      document.body.style.position = '';
      document.body.style.width = '';
      document.body.style.top = '';
      window.scrollTo(0, bodyScrollY.value);
    }
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var modals = document.querySelectorAll('.modal-overlay:not(.hidden)');
    if (modals.length === 0) return;
    modalClose(modals[modals.length - 1].id);
  });

  document.addEventListener('click', function (e) {
    if (!e.target.classList.contains('modal-overlay')) return;
    modalClose(e.target.id);
  });

  /* ============================
   *  5. 表单校验工具
   * ============================ */
  function showFieldError(fieldId, msg) {
    var field = document.getElementById(fieldId);
    if (!field) return;
    field.classList.add('field-error');
    field.setAttribute('aria-invalid', 'true');
    var err = field.parentElement.querySelector('.field-error-msg');
    if (!err) {
      err = document.createElement('span');
      err.className = 'field-error-msg';
      field.parentElement.appendChild(err);
    }
    err.textContent = msg;
  }

  function clearFieldError(fieldId) {
    var field = document.getElementById(fieldId);
    if (field) {
      field.classList.remove('field-error');
      field.removeAttribute('aria-invalid');
    }
    var err = field && field.parentElement.querySelector('.field-error-msg');
    if (err) err.textContent = '';
  }

  function clearAllFieldErrors() {
    document.querySelectorAll('.field-error').forEach(function (f) {
      f.classList.remove('field-error');
      f.removeAttribute('aria-invalid');
    });
    document.querySelectorAll('.field-error-msg').forEach(function (e) { e.textContent = ''; });
  }

  /* ============================
   *  6. Toast 通知（动画优化）
   * ============================ */
  function toast(msg, type) {
    type = type || 'success';
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = msg +
      '<span style="cursor:pointer;margin-left:1rem;opacity:0.6" onclick="App.dismissToast(this.parentElement)">' +
      '<i class="fa-solid fa-xmark"></i></span>';
    document.body.appendChild(el);

    var timer = setTimeout(function () { dismissToast(el); }, 6000);
    el._toastTimer = timer;
  }

  function dismissToast(el) {
    if (!el || el._dismissing) return;
    el._dismissing = true;
    if (el._toastTimer) clearTimeout(el._toastTimer);
    requestAnimationFrame(() => {
      el.classList.add('leaving');
      setTimeout(function () { if (el.parentElement) el.remove(); }, 320);
    });
  }

  /* ============================
   *  7. 数据表格渲染辅助
   * ============================ */
  function renderEmpty(colspan, msg) {
    msg = msg || '暂无数据';
    return '<tr><td colspan="' + colspan + '" class="text-center text-muted p-3">' +
           '<i class="fa-solid fa-folder-open d-block mb-2" style="font-size:1.5rem;opacity:0.4"></i>' +
           escapeHtml(msg) + '</td></tr>';
  }

  function renderLoading(colspan) {
    return '<tr><td colspan="' + colspan + '" class="text-center text-muted p-3">' +
           '<div class="skeleton" style="height:24px;width:60%;margin:0 auto"></div>' +
           '</td></tr>';
  }

  function renderError(colspan, msg) {
    return '<tr><td colspan="' + colspan + '" class="text-center text-danger p-3">' +
           '<i class="fa-solid fa-circle-exclamation d-block mb-2" style="font-size:1.5rem;opacity:0.5"></i>' +
           escapeHtml(msg) + '</td></tr>';
  }

  /* ============================
   *  8. 格式化工具
   * ============================ */
  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = Math.floor(Math.log(bytes) / Math.log(1024));
    if (i >= units.length) i = units.length - 1;
    return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
  }

  function formatDate(isoStr) {
    if (!isoStr) return '-';
    return isoStr.replace('T', ' ').substring(0, 19);
  }

  /* ============================
   *  9. 响应式辅助
   * ============================ */
  function isMobile() {
    return window.matchMedia('(max-width: 768px)').matches;
  }

  function onBreakpoint(callback) {
    var mq = window.matchMedia('(max-width: 768px)');
    mq.addEventListener('change', function (e) { callback(e.matches); });
    callback(mq.matches);
  }

  /* ============================
   *  10. 滚动驱动渐显（默认关闭）
   * ============================ */
  function initScrollReveal() {
    // 在移动端或低性能设备上建议不要开启，若需使用请手动调用
    if (!('IntersectionObserver' in window) || isMobile()) return;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -30px 0px' });

    function markAndObserve() {
      var contentEl = document.querySelector('.content');
      if (!contentEl) return;
      var cards = contentEl.querySelectorAll('.card, .card-stat, .library-card, .list-item, .alert');
      cards.forEach(function (card, i) {
        if (!card.classList.contains('reveal-on-scroll')) {
          card.classList.add('reveal-on-scroll');
        }
        card.style.transitionDelay = (i * 0.04) + 's';
        observer.observe(card);
      });
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', markAndObserve);
    } else {
      markAndObserve();
    }
  }

  /* ============================
   *  暴露为全局变量
   * ============================ */
  window.App = {
    api: api,
    toast: toast,
    dismissToast: dismissToast,
    escapeHtml: escapeHtml,
    modalOpen: modalOpen,
    modalClose: modalClose,
    renderPagination: renderPagination,
    renderEmpty: renderEmpty,
    renderLoading: renderLoading,
    renderError: renderError,
    showFieldError: showFieldError,
    clearFieldError: clearFieldError,
    clearAllFieldErrors: clearAllFieldErrors,
    formatBytes: formatBytes,
    formatDate: formatDate,
    isMobile: isMobile,
    onBreakpoint: onBreakpoint,
    initScrollReveal: initScrollReveal,
  };

  // 向后兼容
  window.api = api;
  window.toast = toast;
  window.dismissToast = dismissToast;
  window.escapeHtml = escapeHtml;
  window.modalOpen = modalOpen;
  window.modalClose = modalClose;
})();