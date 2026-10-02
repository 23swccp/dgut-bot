// Passive observer and bounded structural snapshot. Never clicks, focuses, scrolls or submits.
(function (key, mode, maxNodes) {
  let state = window[key];
  if (mode === 'cleanup') {
    if (state) { state.observer.disconnect(); for (const [name, listener] of state.listeners) document.removeEventListener(name, listener, true); delete window[key]; }
    return true;
  }
  if (!state) {
    state = {ids: new WeakMap(), next: 0, events: [], dropped: 0, listeners: [], revision: 0};
    state.id = node => { if (!state.ids.has(node)) state.ids.set(node, ++state.next); return state.ids.get(node); };
    state.push = event => { state.revision++; if (state.events.length >= 1000) { state.dropped++; return; } state.events.push({...event, at: performance.now()}); };
    for (const name of ['click', 'input', 'change', 'focusin', 'scroll', 'play', 'pause', 'ended', 'error']) {
      const listener = e => { const n = e.target; state.push({type: name, node: n ? state.id(n) : 0,
        trusted: e.isTrusted, x: e.clientX, y: e.clientY, scrollX, scrollY}); };
      document.addEventListener(name, listener, true); state.listeners.push([name, listener]);
    }
    state.observer = new MutationObserver(records => state.push({type: 'mutation', count: records.length,
      nodes: [...new Set(records.slice(0, 20).map(r => state.id(r.target)))],
      attributes: [...new Set(records.map(r => r.attributeName).filter(Boolean))].slice(0, 20)}));
    state.observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    Object.defineProperty(window, key, {value: state, configurable: true});
  }
  if (mode === 'observe') return true;
  if (mode === 'drain') return {events: state.events.splice(0), dropped: state.dropped, revision: state.revision};
  const nodes = [], limits = {nodes: false, depth: false};
  const styles = ['display', 'visibility', 'opacity', 'position', 'width', 'height', 'box-sizing',
    'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'margin-top', 'margin-bottom',
    'font-size', 'line-height', 'font-weight', 'color', 'background-color', 'border-width', 'border-style',
    'border-color', 'flex-direction', 'flex-wrap', 'gap', 'align-items', 'justify-content', 'overflow-x', 'overflow-y'];
  function visit(n, parent, depth) {
    if (nodes.length >= maxNodes) { limits.nodes = true; return; }
    if (depth > 70) { limits.depth = true; return; }
    if (n.nodeType === Node.TEXT_NODE) {
      if (n.textContent.trim()) {
        if (n.textContent.length > 4096) limits.text = true;
        nodes.push({node: state.id(n), parent, tag: '#text', text: n.textContent.slice(0, 4096), originalTextLength: n.textContent.length});
      }
      return;
    }
    if (n.nodeType !== Node.ELEMENT_NODE || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(n.tagName)) return;
    const id = state.id(n), r = n.getBoundingClientRect(), css = getComputedStyle(n);
    const attrs = {};
    for (const name of ['id', 'class', 'type', 'role', 'name', 'data-bind', 'aria-label', 'aria-disabled', 'aria-checked',
      'aria-selected', 'aria-expanded', 'contenteditable', 'placeholder', 'src', 'href', 'for', 'colspan', 'rowspan']) {
      if (n.hasAttribute(name)) {
        const value = n.getAttribute(name);
        if (value.length > 4096) limits.attributes = true;
        attrs[name] = value.slice(0, 4096);
      }
    }
    const item = {node: id, parent, tag: n.tagName.toLowerCase(), attrs,
      rect: {x: r.x, y: r.y, w: r.width, h: r.height}, styles: Object.fromEntries(styles.map(k => [k, css.getPropertyValue(k)])),
      visible: r.width > 0 && r.height > 0 && css.visibility !== 'hidden' && css.display !== 'none',
      disabled: Boolean(n.disabled), hidden: n.hidden, checked: Boolean(n.checked), focused: n === document.activeElement,
      value: 'value' in n && !['BUTTON', 'LI', 'PROGRESS', 'METER'].includes(n.tagName) ? String(n.value || '').slice(0, 4096) : null,
      scroll: {top: n.scrollTop, height: n.scrollHeight, clientHeight: n.clientHeight}};
    if (item.value !== null && String(n.value || '').length > 4096) limits.inputValue = true;
    if (['img', 'canvas', 'svg', 'math'].includes(n.tagName.toLowerCase())) item.visual = {kind: n.tagName.toLowerCase(), width: r.width, height: r.height};
    if (n instanceof HTMLMediaElement) item.media = {paused: n.paused, ended: n.ended, readyState: n.readyState,
      currentTime: n.currentTime, duration: Number.isFinite(n.duration) ? n.duration : null, playbackRate: n.playbackRate, errorCode: n.error?.code || null};
    nodes.push(item);
    for (const child of n.childNodes) { visit(child, id, depth + 1); if (limits.nodes) break; }
    if (n.shadowRoot) {
      item.openShadowRoot = true;
      state.observer.observe(n.shadowRoot, {subtree: true, childList: true, attributes: true, characterData: true});
      for (const child of n.shadowRoot.childNodes) { visit(child, id, depth + 1); if (limits.nodes) break; }
    }
  }
  visit(document.body || document.documentElement, null, 0);
  // Only known KO observables are read; ordinary methods/getters are never invoked.
  function read(value) {
    if (typeof value !== 'function') return value;
    if (window.ko && typeof window.ko.isObservable === 'function' && window.ko.isObservable(value) && typeof value.peek === 'function') return value.peek();
    return {unreadFunction: true};
  }
  function model(value, depth) {
    try { value = read(value); } catch (_) { return {unavailable: 'observable-read-failed'}; }
    if (value === null || typeof value !== 'object') return value;
    if (depth === 0) return {kind: 'object', keys: Object.keys(value).slice(0, 80)};
    if (Array.isArray(value)) return {kind: 'array', length: value.length, samples: value.slice(0, 4).map(v => model(v, depth - 1))};
    const out = {};
    for (const field of ['unreadFunction', 'id', 'name', 'contentType', 'record', 'status', 'chapters', 'sections', 'pages', 'isHide']) {
      const descriptor = Object.getOwnPropertyDescriptor(value, field);
      if (descriptor && 'value' in descriptor) out[field] = model(descriptor.value, depth - 1);
      else if (descriptor) out[field] = {unavailable: 'accessor-not-invoked'};
    }
    return out;
  }
  const viewModel = {}, root = window.koLearnCourseViewModel;
  for (const name of ['course', 'currentPage', 'currentChapter', 'currentSection', 'nextPageName', 'modalType']) {
    const descriptor = root && Object.getOwnPropertyDescriptor(root, name);
    if (descriptor && 'value' in descriptor) viewModel[name] = model(descriptor.value, 4);
    else if (descriptor) viewModel[name] = {unavailable: 'accessor-not-invoked'};
  }
  return {url: location.href, documentEpoch: performance.timeOrigin, readyState: document.readyState, viewport: {width: innerWidth, height: innerHeight, scrollX, scrollY,
    scale: window.visualViewport?.scale || 1}, nodes, limits, viewModel,
    features: {iframes: document.querySelectorAll('iframe,frame').length, images: document.images.length,
      videos: document.querySelectorAll('video,audio').length, questions: document.querySelectorAll('.question-wrapper').length},
    revision: state.revision};
})
