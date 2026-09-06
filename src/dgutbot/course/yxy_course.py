"""优学院课件学习辅助模块。

通过 Chromium DevTools Protocol 连接用户已经打开的课件页，读取真实页面与
媒体状态，控制视频播放和文档滚动，并在页面明确允许后执行一次可验证的导航。

测验自动作答由 yxy_quiz.QuizHandler 承担（"题库学习"策略，可通过
CourseConfig.quiz_auto_answer 关闭）；本模块只负责检测未完成测验并上报
事件，不读取题目答案。课程动作只会放缓原本已经通过校验的点击并加入
细微轨迹变化；不会随机乱点、随机按键或制造无意义的在线活动。
所有点击均经 CDP Input 域产生真实事件。
"""

from __future__ import annotations

import json
import math
import queue
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests
from websocket import create_connection

from dgutbot.course.yxy_quiz import QuizHandler
from dgutbot.course.course_dialogs import DIALOG_POLICY_JS, handle_dialog
from dgutbot.course.course_slides import SLIDE_READER_JS, SLIDE_STATE_JS, frame_point


COURSE_TAB_URL_KEYWORD = "ua.dgut.edu.cn/learnCourse"
EVENT_PREFIX = "[yxy:event]"


# 启动时只读 Knockout 课程目录，并通过真实 CDP 点击目录中的第一张页面。
# 不能调用 view-model 的切页方法，否则页面看似切换但平台可能不记录学习行为。
FIRST_PAGE_TARGET_JS = r"""
(function() {
  try {
    const observed = function(value) { return typeof value === 'function' ? value() : value; };
    const root = window.koLearnCourseViewModel;
    const current = root && observed(root.currentPage);
    const course = root && observed(root.course);
    let first = null;
    for (const chapter of observed(course && course.chapters) || []) {
      for (const section of observed(chapter.sections) || []) {
        for (const page of observed(section.pages) || []) {
          if (!observed(page.isHide)) { first = page; break; }
        }
        if (first) break;
      }
      if (first) break;
    }
    if (!first) return null;
    const pageId = String(observed(first.id) || '');
    const pageName = String(observed(first.name) || '');
    const currentId = String(current && observed(current.id) || '');
    if (!pageId) return null;
    if (pageId === currentId) return {alreadyCurrent: true, page: pageId, pageName: pageName};
    const element = document.getElementById('page' + pageId);
    if (!element) return null;
    element.scrollIntoView({block: 'center', behavior: 'instant'});
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (!rect.width || !rect.height || x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight ||
        !(hit && (hit === element || element.contains(hit)))) return null;
    return {page: pageId, pageName: pageName, x: x, y: y};
  } catch (error) {
    return null;
  }
})()
"""


# 页面脚本只负责观察、视频设置和同源文档滚动。所有导航点击均由 Python 侧的
# ActionExecutor 通过 CDP Input 域执行，脚本本身不直接点击任何元素。
INJECT_JS = r"""
(function(config) {
  /* COURSE_DIALOG_POLICY */
  if (window.__yxy_controller && typeof window.__yxy_controller.cleanup === 'function') {
    window.__yxy_controller.cleanup();
  }

  const C = config;
  window.__yxy_slide_progress = {};
  const sessionToken = String(C.session_token || '');
  let running = true;
  let videoSpeed = Number(C.playback_rate) || 1;
  let observer = null;
  let mutationTimeout = null;
  let currentVideo = null;
  let currentVideos = [];
  let lastPageSignature = '';
  let lastNextNotice = '';
  let lastQuizNotice = '';
  let documentTarget = null;
  let documentScrolled = false;
  let documentNotice = '';
  let videoPageNotice = '';
  let videoPlayingNotice = '';
  let staticPageNotice = '';
  let stablePageSince = Date.now();
  let quizClearSince = 0;
  const intervals = new Set();
  const timeouts = new Set();
  const listeners = [];
  const hookedVideos = new WeakSet();
  const completedContent = new Set();
  const completedVideos = window.__yxy_completed_videos instanceof Set
    ? window.__yxy_completed_videos : new Set();
  window.__yxy_completed_videos = completedVideos;
  const videoProgress = new WeakMap();

  function log(message) {
    if (running) console.log('[yxy:' + sessionToken + '] ' + message);
  }

  function event(type, details) {
    if (!running) return;
    const page = entityState('currentPage');
    const payload = Object.assign({type: type, session: sessionToken, page: page.id}, details || {});
    console.log('[yxy:event] ' + JSON.stringify(payload));
  }

  function ownInterval(callback, delay) {
    const id = setInterval(callback, delay);
    intervals.add(id);
    return id;
  }

  function ownTimeout(callback, delay) {
    const id = setTimeout(function() {
      timeouts.delete(id);
      callback();
    }, delay);
    timeouts.add(id);
    return id;
  }

  function listen(target, name, callback) {
    target.addEventListener(name, callback);
    listeners.push([target, name, callback]);
  }

  function visible(element) {
    if (!element || !element.isConnected) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
  }

  function disabled(element) {
    return Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true' ||
      element.classList.contains('disabled') || element.classList.contains('is-disabled'));
  }

  function compactText(element) {
    return ((element && element.textContent) || '').replace(/\s+/g, ' ').trim();
  }

  function stableHash(value) {
    let hash = 2166136261;
    const text = String(value || '');
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16);
  }

  function safeUrl(value, includeHash) {
    try {
      const parsed = new URL(String(value || ''), location.href);
      const hash = includeHash && parsed.hash ? '#route-' + stableHash(parsed.hash) : '';
      return parsed.origin + parsed.pathname + hash;
    } catch (error) {
      return '';
    }
  }

  function mediaIdentity(value) {
    const raw = String(value || '');
    return raw ? 'media-' + stableHash(raw) : '';
  }

  function chapterIdentity() {
    const selected = document.querySelector(
      '[aria-current="page"], [aria-current="true"], .chapter-item.active, ' +
      '.catalog-item.active, .course-directory .active, li.active'
    );
    if (!selected) return '';
    const stableId = selected.getAttribute('data-id') || selected.getAttribute('data-chapter-id') ||
      selected.id || selected.getAttribute('href') || '';
    return 'chapter-' + stableHash(stableId || compactText(selected).slice(0, 120));
  }

  function observed(value) {
    try { return typeof value === 'function' ? value() : value; } catch (error) { return null; }
  }

  function entityState(name) {
    const root = window.koLearnCourseViewModel;
    const entity = root ? observed(root[name]) : null;
    if (!entity) return {id: '', name: ''};
    return {id: String(observed(entity.id) || ''), name: String(observed(entity.name) || '')};
  }

  function currentPageRecordComplete() {
    try {
      const root = window.koLearnCourseViewModel;
      const page = root && observed(root.currentPage);
      const record = page && observed(page.record);
      return Boolean(record && observed(record.status));
    } catch (error) {
      return false;
    }
  }

  function currentContentType() {
    try {
      const root = window.koLearnCourseViewModel;
      const page = root && observed(root.currentPage);
      return Number(page && observed(page.contentType)) || 0;
    } catch (error) {
      return 0;
    }
  }

  function pageState() {
    const page = entityState('currentPage');
    const chapter = entityState('currentChapter');
    const section = entityState('currentSection');
    const videos = Array.from(document.querySelectorAll('video'));
    let courseName = '';
    let pageIndex = 0;
    let pageTotal = 0;
    try {
      const root = window.koLearnCourseViewModel;
      const course = root && observed(root.course);
      courseName = String(course && observed(course.name) || '');
      const chapters = course && observed(course.chapters) || [];
      const pages = [];
      for (const item of chapters) {
        for (const child of observed(item.sections) || []) {
          for (const candidate of observed(child.pages) || []) {
            if (!observed(candidate.isHide)) pages.push(candidate);
          }
        }
      }
      pageTotal = pages.length;
      pageIndex = pages.findIndex(function(candidate) {
        return String(observed(candidate.id) || '') === page.id;
      }) + 1;
    } catch (error) {}
    return {
      url: safeUrl(location.href, true),
      chapter: chapter.id || chapterIdentity(),
      chapterName: chapter.name,
      section: section.id,
      sectionName: section.name,
      page: page.id,
      pageName: page.name,
      courseName: courseName,
      pageIndex: pageIndex,
      pageTotal: pageTotal,
      contentType: currentContentType(),
      recordComplete: currentPageRecordComplete(),
      source: videos.map(function(video) { return mediaIdentity(video.currentSrc || video.src || ''); }).join(',')
    };
  }

  function pageSignature(state) {
    const value = state || pageState();
    return [value.url || '', value.chapter || '', value.section || '', value.page || '', value.pageName || ''].join('\n');
  }

  function safeTarget(element, kind) {
    if (!visible(element) || disabled(element)) return null;
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return null;
    const hit = document.elementFromPoint(x, y);
    const pointMatches = Boolean(hit && (hit === element || element.contains(hit)));
    if (!pointMatches) return null;
    return {
      kind: kind,
      x: x,
      y: y,
      width: rect.width,
      height: rect.height,
      pointMatches: pointMatches,
      disabled: false,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight
    };
  }

  function networkDialogElement() {
    const dialog = document.getElementById('alertModal');
    if (!visible(dialog)) return null;
    const root = window.koLearnCourseViewModel;
    const kind = root && observed(root.modalType);
    if (kind !== 'docNoWifi' && kind !== 'videoNoWifi') return null;
    const text = compactText(dialog);
    return /不是\s*wi[-\s]?fi\s*网络/i.test(text) && /消耗流量/.test(text) ? dialog : null;
  }

  function forwardDialogTarget() {
    if (!C.auto_dismiss_dialog) return null;
    const dialog = courseDialogState();
    return dialog && dialog.policy === 'navigation' && dialog.target
      ? Object.assign({}, dialog.target, {kind: 'forward-dialog'}) : null;
  }

  function completionDialogTarget() {
    const controls = document.querySelectorAll('button, a, [role="button"]');
    for (const control of controls) {
      if (!/^继续\s*下\s*一章$/.test(compactText(control))) continue;
      const container = control.closest(
        '[role="dialog"], .modal, .dialog, .el-message-box, .ant-modal, .layui-layer, .popup'
      );
      if (!container || !visible(container)) continue;
      const text = compactText(container);
      if (/长时间无操作|本人确认|身份验证|验证码|请确认在场/.test(text)) continue;
      if (!/恭喜你完成本章|完成本章的学习|本章成绩/.test(text)) continue;
      const target = safeTarget(control, 'completion-dialog');
      if (target) return target;
    }
    return null;
  }

  function explicitNextTarget() {
    const selectors = ['.next-btn', '.btn-next', '.nextVideoBtn', '.mobile-next-page-btn', '.next-page-btn'];
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      const target = safeTarget(element, 'explicit-next');
      if (target) return target;
    }
    return null;
  }

  function getNavigationTarget() {
    if (quizStartPending() && !quizSkipBeforeStart()) return null;
    if (slideDocuments().some(item => !item.completed)) return null;
    if (!C.auto_next) return null;
    const dialog = courseDialogState();
    if (dialog) return forwardDialogTarget();
    return forwardDialogTarget() || completionDialogTarget() || explicitNextTarget();
  }

  function applyVideoSettings(video, reason) {
    if (!running || !video || !video.isConnected) return;
    video.muted = Boolean(C.auto_mute);
    if (Math.abs((video.playbackRate || 1) - videoSpeed) > 0.01) {
      video.playbackRate = videoSpeed;
    }
    if (currentPageRecordComplete()) {
      if (!video.paused) video.pause();
      return;
    }
    if (C.auto_play && video === currentVideo && video.paused && !video.ended && video.readyState >= 2 && !quizBusy()) {
      video.play().catch(function() {});
    }
  }

  function videosOnPage() {
    return Array.from(document.querySelectorAll('video')).filter(function(video) {
      return video && video.isConnected && Number(video.duration || 0) >= 0;
    });
  }

  function videoItemKey(video, videos) {
    const list = videos || currentVideos || videosOnPage();
    const index = Math.max(0, list.indexOf(video));
    const page = entityState('currentPage');
    return (page.id || stableHash(pageSignature())) + ':video-' + index;
  }

  function videoLogicallyFinished(video, videos) {
    return completedVideos.has(videoItemKey(video, videos)) || video.ended ||
      (Number.isFinite(video.duration) && video.duration > 0 && video.currentTime >= video.duration - 0.35);
  }

  function videoPageKey() {
    const state = pageState();
    return 'page-' + (state.page || stableHash(pageSignature(state)));
  }

  function allVideosFinished(videos) {
    return videos.length > 0 && videos.every(function(video) {
      return videoLogicallyFinished(video, videos);
    });
  }

  function syncVideoSequence(reason) {
    const videos = videosOnPage();
    currentVideos = videos;
    for (const video of videos) hookVideo(video);
    // 目录记录已完成的页面不应从 0 秒重新播放。平台会在视频结束后重置
    // currentTime，因此这里把服务端完成记录同步到本页视频完成账本。
    if (currentPageRecordComplete()) {
      for (const video of videos) {
        completedVideos.add(videoItemKey(video, videos));
        if (!video.paused) video.pause();
      }
    }
    currentVideo = videos.find(function(video) { return !videoLogicallyFinished(video, videos); }) || null;
    for (const video of videos) applyVideoSettings(video, reason);
    const key = videoPageKey();
    if (videos.length && videoPageNotice !== key) {
      videoPageNotice = key;
      event('video-ready', {source: key, total: videos.length});
    }
    if (currentVideo && !currentVideo.paused && videoPlayingNotice !== key) {
      videoPlayingNotice = key;
      event('video-playing', {source: key});
    }
    if (allVideosFinished(videos) && !completedContent.has('video:' + key)) {
      completedContent.add('video:' + key);
      event('video-ended', {source: key, total: videos.length, chapter: entityState('currentChapter').id});
      log('本页 ' + videos.length + ' 个视频均已播放结束，等待进入下一页');
    }
  }

  function hookVideo(video) {
    if (hookedVideos.has(video)) return;
    hookedVideos.add(video);
    const progress = {
      source: String(video.currentSrc || video.src || ''),
      lastTime: Number(video.currentTime || 0),
      lastGrowthAt: Date.now(),
      recoveries: 0,
      hasPlayed: false
    };
    videoProgress.set(video, progress);

    listen(video, 'loadstart', function() {
      const state = videoProgress.get(video);
      if (state) {
        state.source = String(video.currentSrc || video.src || '');
        state.lastTime = Number(video.currentTime || 0);
        state.lastGrowthAt = Date.now();
        state.recoveries = 0;
        state.hasPlayed = false;
      }
    });
    listen(video, 'loadedmetadata', function() { syncVideoSequence('loadedmetadata'); });
    listen(video, 'canplay', function() { syncVideoSequence('canplay'); });
    listen(video, 'play', function() {
      const state = videoProgress.get(video);
      if (state) {
        state.hasPlayed = true;
        state.lastGrowthAt = Date.now();
      }
      event('video-playing', {source: videoPageKey()});
      applyVideoSettings(video, 'play');
    });
    listen(video, 'ratechange', function() {
      if (running && Math.abs((video.playbackRate || 1) - videoSpeed) > 0.01) {
        applyVideoSettings(video, 'ratechange');
      }
    });
    listen(video, 'ended', function() {
      completedVideos.add(videoItemKey(video, currentVideos));
      log('一个视频已播放结束，正在检查本页其余视频');
      syncVideoSequence('ended');
    });
    listen(video, 'error', function() {
      event('video-error', {source: mediaIdentity(video.currentSrc || video.src || ''), reason: 'media-error'});
    });
    applyVideoSettings(video, 'hook');
  }

  function watchVideo() {
    syncVideoSequence('watch');
    if (!running || !currentVideo || !currentVideo.isConnected) return;
    const video = currentVideo;
    const state = videoProgress.get(video);
    if (!state) return;
    if (quizBusy()) {
      state.lastGrowthAt = Date.now();
      return;
    }
    const source = String(video.currentSrc || video.src || '');
    const now = Date.now();
    const currentTime = Number(video.currentTime || 0);
    if (source !== state.source) {
      state.source = source;
      state.lastTime = currentTime;
      state.lastGrowthAt = now;
      state.recoveries = 0;
      state.hasPlayed = false;
      return;
    }
    if (document.hidden || video.ended || video.readyState < 3 || !state.hasPlayed) {
      state.lastTime = currentTime;
      state.lastGrowthAt = now;
      return;
    }
    if (currentTime > state.lastTime + 0.15) {
      state.lastTime = currentTime;
      state.lastGrowthAt = now;
      state.recoveries = 0;
      return;
    }
    if (now - state.lastGrowthAt < 15000) return;
    if (state.recoveries >= 2) {
      event('video-error', {source: videoPageKey(), reason: 'stalled-after-retries', currentTime: currentTime});
      state.lastGrowthAt = now;
      return;
    }
    state.recoveries += 1;
    state.lastGrowthAt = now;
    event('video-stalled', {source: videoPageKey(), currentTime: currentTime, recovery: state.recoveries});
    video.pause();
    if (Number.isFinite(video.duration) && currentTime < video.duration - 0.2) {
      video.currentTime = Math.min(video.duration - 0.1, currentTime + 0.1);
    }
    video.play().catch(function() {});
  }

  function isSidebar(element) {
    if (!element) return false;
    const marker = String(element.className || '') + ' ' + String(element.id || '');
    return /sidebar|catalog|outline|menu|toc|directory|left-nav|chapter-list|catalogue/i.test(marker) ||
      (element.clientWidth > 0 && element.clientWidth < 320);
  }

  function findDocumentTarget() {
    // 未启动测验的长题目列表不是待阅读文档；独立的文档容器仍照常识别。
    const pendingQuizViews = Array.from(document.querySelectorAll('.question-view'))
      .filter(view => Array.from(view.querySelectorAll('.limit-time-mask')).some(visible));
    const candidates = [document.scrollingElement, document.documentElement, document.body]
      .concat(Array.from(document.querySelectorAll(
        'main, [class*="content"], [class*="learnContent"], [class*="courseContent"], ' +
        '[class*="main-content"], [class*="doc-content"], [class*="page-content"]'
      )))
      .filter(function(element) {
        return element && visible(element) && !isSidebar(element) &&
          !pendingQuizViews.some(view => element.contains(view) || view.contains(element)) &&
          element.scrollHeight > element.clientHeight + 2;
      })
      .sort(function(a, b) {
        return (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight);
      });
    return candidates[0] || null;
  }

  function scrollSameOriginDocument() {
    if (!running || !C.document_scroll_enabled) return;
    const pageVideos = videosOnPage();
    if (pageVideos.some(function(video) { return !videoLogicallyFinished(video, pageVideos); })) return;
    if (quizBusy()) return;
    if (document.querySelector('iframe[src*="ulearning.cn"]')) return;
    const target = findDocumentTarget();
    if (!target) return;
    if (target !== documentTarget) {
      documentTarget = target;
      documentScrolled = false;
      documentNotice = '';
    }
    const key = pageSignature();
    if (documentNotice !== key) {
      documentNotice = key;
      event('document-reading', {chapter: chapterIdentity(), url: safeUrl(location.href, true)});
      log('检测到文档内容，开始分段滚动阅读');
    }
    const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
    const remaining = maxTop - target.scrollTop;
    if (remaining <= 2) {
      if (documentScrolled && !completedContent.has('document:' + key)) {
        completedContent.add('document:' + key);
        event('document-bottom', {chapter: chapterIdentity(), url: safeUrl(location.href, true)});
        log('文档已滚动至末尾，等待页面确认可进入下一节');
      }
      return;
    }
    const oldTop = target.scrollTop;
    const step = Math.max(160, Math.min(560, Math.round(target.clientHeight * 0.65)));
    target.scrollTop = Math.min(oldTop + step, maxTop);
    if (target.scrollTop > oldTop) documentScrolled = true;
  }

  function quizUnfinishedCount() {
    const view = document.querySelector('.question-view');
    if (!view || !visible(view)) return -1;
    let count = 0;
    document.querySelectorAll('.question-wrapper').forEach(function(w) {
      if (!w.classList.contains('finished')) count += 1;
    });
    return quizStartPending() ? Math.max(1, count) : count;
  }

  // 只读题型摘要，供 Python 侧把一页拆成可组合的任务计划；不参与作答。
  function quizTypeSummary() {
    const view = document.querySelector('.question-view');
    if (!view || !visible(view)) return {};
    const result = {};
    document.querySelectorAll('.question-wrapper:not(.finished)').forEach(function(w) {
      const tag = compactText(w.querySelector('.question-type-tag')) || '未知题型';
      result[tag] = Number(result[tag] || 0) + 1;
    });
    return result;
  }

  function quizLoading() {
    const view = document.querySelector('.question-view');
    return Boolean(view && visible(view) && !view.querySelector('.question-wrapper') && !currentPageRecordComplete());
  }

  // 限时测验题目已渲染，但开始前被遮罩覆盖；启动动作交给 Python。
  function quizStartPending() {
    return Array.from(document.querySelectorAll('.question-view .limit-time-mask')).some(visible);
  }

  function quizSkipBeforeStart() {
    return (C.quiz_auto_answer === false || C.quiz_mode === 'disabled') && quizStartPending();
  }

  function quizBusy() {
    if (window.__yxy_agent_waiting) return true;
    if (quizLoading()) return true;
    if (quizSkipBeforeStart()) return false;
    return quizUnfinishedCount() > 0;
  }

  function nextPageName() {
    try {
      const root = window.koLearnCourseViewModel;
      return String(root && observed(root.nextPageName) || '');
    } catch (error) {
      return '';
    }
  }

  function courseFinished() {
    const next = nextPageName().trim();
    if (/^(没有了|无|none)$/i.test(next)) return true;
    if (next !== '本章统计') return false;
    try {
      const root = window.koLearnCourseViewModel;
      const course = root && observed(root.course);
      const chapters = course && observed(course.chapters) || [];
      const pages = [];
      for (const chapter of chapters) {
        for (const section of observed(chapter.sections) || []) {
          for (const page of observed(section.pages) || []) {
            if (!observed(page.isHide)) pages.push(page);
          }
        }
      }
      const current = root && observed(root.currentPage);
      return Boolean(current && pages.length && String(observed(pages[pages.length - 1].id)) === String(observed(current.id)));
    } catch (error) {
      return false;
    }
  }

  function slideDocuments() {
    const page = pageState().page;
    return Array.from(document.querySelectorAll('.doc-wrapper')).filter(wrapper =>
      wrapper.querySelector('.doc-card.ppt') && visible(wrapper.querySelector('iframe.doc-iframe'))
    ).map(wrapper => {
      const resource = stableHash(wrapper.querySelector('iframe.doc-iframe').src);
      const progress = (window.__yxy_slide_progress || {})[resource];
      return progress && progress.page === page ? progress : {resource: resource, current: 0, total: 0, completed: false};
    });
  }

  // “没有下一页”只说明处于末页，不能作为课件已完成的证据。
  // 快照和事件共用这个判据，防止看门狗绕过题目异步加载/媒体完成检查。
  function courseCompletionReady() {
    if (!courseFinished() || courseDialogState() || quizBusy() || quizStartPending() ||
        slideDocuments().some(item => !item.completed)) return false;
    if (currentPageRecordComplete()) return true;
    if (Date.now() - stablePageSince < 2500 || (quizClearSince && Date.now() - quizClearSince < 2500)) return false;
    const videos = videosOnPage();
    if (videos.length) return allVideosFinished(videos);
    const documentDone = !findDocumentTarget() || completedContent.has('document:' + pageSignature());
    return documentDone && (!document.querySelector('iframe[src*="ulearning.cn"]') || currentContentType() === 5);
  }

  function statusSnapshot() {
    const state = pageState();
    const videos = videosOnPage();
    const dialog = forwardDialogTarget();
    const dialogState = courseDialogState();
    return {
      state: state,
      nextPageName: nextPageName(),
      courseFinished: courseCompletionReady(),
      slideDocuments: slideDocuments(),
      dialogState: dialogState,
      networkDialogPending: Boolean(networkDialogElement()),
      quizUnfinished: quizUnfinishedCount(),
      quizLoading: quizLoading(),
      quizTypes: quizTypeSummary(),
      quizStartPending: quizStartPending(),
      quizSkipBeforeStart: quizSkipBeforeStart(),
      hasDocument: Boolean(findDocumentTarget()),
      hasDocumentFrame: Boolean(document.querySelector('iframe[src*="ulearning.cn"]')),
      dialog: dialog ? dialog.kind : '',
      videos: videos.map(function(video, index) {
        return {
          index: index,
          currentTime: Math.round(Number(video.currentTime || 0) * 10) / 10,
          duration: Number.isFinite(video.duration) ? Math.round(video.duration * 10) / 10 : null,
          paused: Boolean(video.paused),
          ended: videoLogicallyFinished(video, videos),
          readyState: Number(video.readyState || 0),
          rate: Number(video.playbackRate || 1)
        };
      })
    };
  }

  function tick() {
    if (!running) return;
    const state = pageState();
    const signature = pageSignature(state);
    const changed = Boolean(lastPageSignature && signature !== lastPageSignature);
    if (changed) {
      event('chapter-changed', {from: lastPageSignature, to: signature, state: state});
      lastNextNotice = '';
      documentTarget = null;
      documentScrolled = false;
      documentNotice = '';
      videoPageNotice = '';
      videoPlayingNotice = '';
      staticPageNotice = '';
      stablePageSince = Date.now();
      quizClearSince = 0;
    }
    lastPageSignature = signature;

    // 流量确认在加载阶段就应处理，不能等待“当前页完成”再进入翻页流程。
    const pendingDialog = courseDialogState();
    if (pendingDialog && pendingDialog.policy !== 'navigation') return;

    const skipQuiz = quizSkipBeforeStart();
    const quizUnfinished = skipQuiz ? 0 : quizUnfinishedCount();
    if (quizUnfinished > 0) {
      quizClearSince = 0;
      const quizNotice = signature + '#' + quizUnfinished;
      if (quizNotice !== lastQuizNotice) {
        lastQuizNotice = quizNotice;
        event('quiz-appeared', {unfinished: quizUnfinished, chapter: state.chapter, source: state.source});
        log('检测到未完成测验：' + quizUnfinished + ' 道，自动播放与滚动已挂起');
      }
    } else {
      if (quizUnfinished === 0 && !quizClearSince) quizClearSince = Date.now();
      if (lastQuizNotice && quizUnfinished === 0 && !skipQuiz) {
        event('quiz-finished', {chapter: state.chapter, source: state.source});
        log('测验题目已全部完成');
      }
      lastQuizNotice = '';
    }

    syncVideoSequence(changed ? 'source-change' : 'tick');

    const videos = videosOnPage();
    const hasDocumentFrame = Boolean(document.querySelector('iframe[src*="ulearning.cn"]'));
    const hasDocument = Boolean(findDocumentTarget());
    const canFinishWithoutMedia = !quizLoading() && quizUnfinished === 0 && !videos.length &&
      !hasDocument && (!hasDocumentFrame || currentContentType() === 5);
    const quizSettled = quizUnfinished < 0 || (quizClearSince && Date.now() - quizClearSince >= 2500);
    const slidesPending = slideDocuments().some(item => !item.completed);
    if (!slidesPending && (currentPageRecordComplete() || canFinishWithoutMedia) && quizSettled && Date.now() - stablePageSince >= 2500) {
      const staticKey = 'page-' + (state.page || stableHash(signature));
      if (staticPageNotice !== staticKey) {
        staticPageNotice = staticKey;
        event('static-ready', {source: staticKey, recordComplete: currentPageRecordComplete(), contentType: currentContentType(), quizSkipped: skipQuiz});
      }
    }

    if (courseCompletionReady()) {
      event('course-finished', {state: state});
    }

    const target = getNavigationTarget();
    const notice = target ? signature + '\n' + target.kind : '';
    if (target && notice !== lastNextNotice) {
      lastNextNotice = notice;
      event('next-ready', {kind: target.kind, chapter: state.chapter, source: state.source});
    } else if (!target) {
      lastNextNotice = '';
    }
  }

  function scheduleTick() {
    if (!running || mutationTimeout !== null) return;
    mutationTimeout = ownTimeout(function() {
      mutationTimeout = null;
      tick();
    }, 250);
  }

  function cleanup() {
    if (!running) return true;
    running = false;
    if (observer) observer.disconnect();
    observer = null;
    for (const id of intervals) clearInterval(id);
    intervals.clear();
    for (const id of timeouts) clearTimeout(id);
    timeouts.clear();
    mutationTimeout = null;
    for (const item of listeners) item[0].removeEventListener(item[1], item[2]);
    listeners.length = 0;
    return true;
  }

  observer = new MutationObserver(scheduleTick);
  observer.observe(document.documentElement || document.body, {childList: true, subtree: true, attributes: true});
  ownInterval(tick, 2000);
  ownInterval(watchVideo, 5000);
  if (C.document_scroll_enabled) {
    const speed = Math.max(1, Math.min(3, Number(C.document_scroll_speed) || 1));
    const base = Math.max(1000, (Number(C.document_scroll_interval) || 3) * 1000);
    ownInterval(scrollSameOriginDocument, Math.max(1000, base / speed));
  }

  window.__yxy_controller = {
    cleanup: cleanup,
    get_navigation_target: getNavigationTarget,
    get_page_state: pageState,
    get_status: statusSnapshot,
    recover: function() {
      if (currentVideo && currentVideo.readyState < 3 && videoSpeed > 2) {
        videoSpeed = Math.max(2, videoSpeed / 2);
        log('检测到持续缓冲，恢复倍速自动降为 ' + videoSpeed + 'x');
      }
      if (currentVideo && !currentVideo.ended) {
        const resumeAt = Number(currentVideo.currentTime || 0);
        currentVideo.pause();
        if (Number.isFinite(currentVideo.duration) && resumeAt < currentVideo.duration - 0.2) {
          currentVideo.currentTime = Math.min(currentVideo.duration - 0.1, resumeAt + 0.1);
        }
      }
      syncVideoSequence('recovery');
      tick();
      return statusSnapshot();
    },
    set_speed: function(rate) {
      const value = Number(rate);
      if (Number.isFinite(value) && value >= 1 && value <= 16) {
        videoSpeed = value;
        if (currentVideo) applyVideoSettings(currentVideo, 'speed-change');
        log('视频倍速已调整为 ' + value + 'x');
        return true;
      }
      return false;
    }
  };
  window.__yxy_stop = cleanup;
  window.__yxy_set_speed = window.__yxy_controller.set_speed;
  tick();
  event('page-ready', {state: pageState()});
  log('课件控制器已启动，倍速 ' + videoSpeed + 'x');
})(window.__YXY_CONFIG__);
""".replace('/* COURSE_DIALOG_POLICY */', DIALOG_POLICY_JS)


@dataclass
class CourseConfig:
    """注入页面控制器所需的兼容配置。"""

    playback_rate: float = 8.0
    auto_play: bool = True
    auto_mute: bool = True
    auto_next: bool = True
    # 自动处理已识别的课程提示，包括非 Wi-Fi 流量确认。
    auto_dismiss_dialog: bool = True
    document_scroll_enabled: bool = True
    document_scroll_interval: float = 3.0
    document_scroll_speed: float = 3.0
    # 测验自动作答：选择题点 C、判断题点"错误"、每空填英文逗号。
    quiz_auto_answer: bool = True
    quiz_mode: str = "fixed"
    quiz_choice_enabled: bool = True
    quiz_judgment_enabled: bool = True
    quiz_blank_enabled: bool = True
    quiz_option_label: str = "C"
    quiz_judgment_label: str = "错误"
    quiz_blank_text: str = ","
    # 防走神：定期用滚轮下滑再上滑（真实 wheel 事件），重置平台挂机计时。
    anti_idle_scroll: bool = True
    anti_idle_interval: float = 90.0

    def to_js(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class CourseState(str, Enum):
    IDLE = "IDLE"
    ATTACHING = "ATTACHING"
    LOADING = "LOADING"
    VIDEO_READY = "VIDEO_READY"
    VIDEO_PLAYING = "VIDEO_PLAYING"
    DOCUMENT_READING = "DOCUMENT_READING"
    CONTENT_FINISHED = "CONTENT_FINISHED"
    WAITING_PAGE_CONFIRM = "WAITING_PAGE_CONFIRM"
    READY_FOR_NEXT = "READY_FOR_NEXT"
    NAVIGATING = "NAVIGATING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class CourseStateMachine:
    """与浏览器无关的课程状态机，负责去重和互斥。"""

    _ALLOWED = {
        CourseState.IDLE: {CourseState.ATTACHING, CourseState.STOPPED},
        CourseState.ATTACHING: {CourseState.LOADING, CourseState.ERROR, CourseState.STOPPED},
        CourseState.LOADING: {
            CourseState.VIDEO_READY,
            CourseState.VIDEO_PLAYING,
            CourseState.DOCUMENT_READING,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.VIDEO_READY: {
            CourseState.VIDEO_PLAYING,
            CourseState.CONTENT_FINISHED,
            CourseState.LOADING,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.VIDEO_PLAYING: {
            CourseState.CONTENT_FINISHED,
            CourseState.LOADING,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.DOCUMENT_READING: {
            CourseState.CONTENT_FINISHED,
            CourseState.LOADING,
            CourseState.VIDEO_READY,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.CONTENT_FINISHED: {CourseState.WAITING_PAGE_CONFIRM, CourseState.ERROR, CourseState.STOPPED},
        CourseState.WAITING_PAGE_CONFIRM: {
            CourseState.READY_FOR_NEXT,
            CourseState.LOADING,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.READY_FOR_NEXT: {
            CourseState.NAVIGATING,
            CourseState.LOADING,
            CourseState.ERROR,
            CourseState.STOPPED,
        },
        CourseState.NAVIGATING: {
            CourseState.LOADING,
            CourseState.WAITING_PAGE_CONFIRM,
            CourseState.ERROR,
            CourseState.COMPLETED,
            CourseState.STOPPED,
        },
        CourseState.PAUSED: {CourseState.LOADING, CourseState.STOPPED},
        CourseState.ERROR: {CourseState.ATTACHING, CourseState.STOPPED},
        CourseState.COMPLETED: {CourseState.ATTACHING, CourseState.STOPPED},
        CourseState.STOPPED: {CourseState.ATTACHING},
    }

    def __init__(self) -> None:
        self.state = CourseState.IDLE
        self.chapter_key = ""
        self.generation = 0
        self.content_kind: str | None = None
        self.content_key = ""
        self._completion_generation: int | None = None
        self._navigation_generation: int | None = None
        self.history: list[tuple[CourseState, CourseState]] = []
        self._lock = threading.RLock()

    def transition(self, new_state: CourseState) -> None:
        with self._lock:
            if new_state == self.state:
                return
            if new_state not in self._ALLOWED[self.state]:
                raise ValueError(f"非法课件状态转换：{self.state.value} -> {new_state.value}")
            old_state = self.state
            self.state = new_state
            self.history.append((old_state, new_state))

    def reset_for_start(self) -> None:
        with self._lock:
            if self.state not in {CourseState.IDLE, CourseState.STOPPED, CourseState.ERROR, CourseState.COMPLETED}:
                raise ValueError(f"控制器仍处于 {self.state.value}")
            self.content_kind = None
            self.content_key = ""
            self._completion_generation = None
            self._navigation_generation = None
            self.transition(CourseState.ATTACHING)

    def observe_video_ready(self, content_key: str = "") -> bool:
        with self._lock:
            if self.state not in {CourseState.LOADING, CourseState.DOCUMENT_READING}:
                return False
            self.content_kind = "video"
            self.content_key = content_key
            self.transition(CourseState.VIDEO_READY)
            return True

    def observe_video_playing(self, content_key: str = "") -> bool:
        with self._lock:
            if self.state not in {CourseState.LOADING, CourseState.VIDEO_READY}:
                return False
            self.content_kind = "video"
            if content_key:
                self.content_key = content_key
            self.transition(CourseState.VIDEO_PLAYING)
            return True

    def observe_document_reading(self, content_key: str = "") -> bool:
        with self._lock:
            if self.state != CourseState.LOADING:
                return False
            self.content_kind = "document"
            self.content_key = content_key
            self.transition(CourseState.DOCUMENT_READING)
            return True

    def prepare_document_after_video(self) -> bool:
        """同页视频完成后，回到加载态以处理后续文档任务。"""
        with self._lock:
            if self.state not in {CourseState.VIDEO_READY, CourseState.VIDEO_PLAYING}:
                return False
            self.content_kind = None
            self.content_key = ""
            self.transition(CourseState.LOADING)
            return True

    def observe_static_ready(self, content_key: str = "") -> bool:
        with self._lock:
            if self.state != CourseState.LOADING:
                return False
            self.content_kind = "static"
            self.content_key = content_key
            self.transition(CourseState.DOCUMENT_READING)
            return True

    def mark_content_finished(self, kind: str, content_key: str = "") -> bool:
        with self._lock:
            expected = {CourseState.VIDEO_READY, CourseState.VIDEO_PLAYING} if kind == "video" else {CourseState.DOCUMENT_READING}
            if self.state not in expected or self.content_kind != kind:
                return False
            if content_key and self.content_key and content_key != self.content_key:
                return False
            if self._completion_generation == self.generation:
                return False
            self._completion_generation = self.generation
            self.transition(CourseState.CONTENT_FINISHED)
            self.transition(CourseState.WAITING_PAGE_CONFIRM)
            return True

    def mark_record_complete(self, content_key: str = "") -> bool:
        """目录记录已明确完成时，无需重播媒体即可进入导航阶段。"""
        with self._lock:
            if self.state in {
                CourseState.WAITING_PAGE_CONFIRM,
                CourseState.READY_FOR_NEXT,
                CourseState.NAVIGATING,
                CourseState.COMPLETED,
                CourseState.STOPPED,
            }:
                return False
            if self._completion_generation == self.generation:
                return False
            old_state = self.state
            self.content_kind = "record"
            self.content_key = content_key
            self._completion_generation = self.generation
            self.state = CourseState.WAITING_PAGE_CONFIRM
            self.history.append((old_state, CourseState.WAITING_PAGE_CONFIRM))
            return True

    def mark_next_ready(self) -> bool:
        with self._lock:
            if self.state != CourseState.WAITING_PAGE_CONFIRM:
                return False
            self.transition(CourseState.READY_FOR_NEXT)
            return True

    def begin_navigation(self) -> bool:
        with self._lock:
            if self.state != CourseState.READY_FOR_NEXT:
                return False
            if self._navigation_generation == self.generation:
                return False
            self._navigation_generation = self.generation
            self.transition(CourseState.NAVIGATING)
            return True

    def navigation_succeeded(self, chapter_key: str) -> None:
        with self._lock:
            if self.state != CourseState.NAVIGATING:
                return
            self.generation += 1
            self.chapter_key = chapter_key
            self.content_kind = None
            self.content_key = ""
            self._completion_generation = None
            self._navigation_generation = None
            self.transition(CourseState.LOADING)

    def navigation_failed(self) -> None:
        with self._lock:
            if self.state != CourseState.NAVIGATING:
                return
            self._navigation_generation = None
            self.transition(CourseState.WAITING_PAGE_CONFIRM)

    def complete(self) -> bool:
        with self._lock:
            if self.state in {CourseState.COMPLETED, CourseState.STOPPED}:
                return False
            old_state = self.state
            self.state = CourseState.COMPLETED
            self.history.append((old_state, CourseState.COMPLETED))
            return True

    def page_changed(self, chapter_key: str) -> None:
        """处理非动作线程观察到的 SPA 换章或换源。"""
        with self._lock:
            if self.state in {CourseState.IDLE, CourseState.ATTACHING, CourseState.STOPPED, CourseState.ERROR}:
                return
            if self.state == CourseState.NAVIGATING:
                self.navigation_succeeded(chapter_key)
                return
            if self.state != CourseState.LOADING:
                self.transition(CourseState.LOADING)
            self.generation += 1
            self.chapter_key = chapter_key
            self.content_kind = None
            self.content_key = ""
            self._completion_generation = None
            self._navigation_generation = None

    def fail(self) -> None:
        with self._lock:
            if self.state not in {CourseState.ERROR, CourseState.STOPPED}:
                self.transition(CourseState.ERROR)

    def pause(self) -> None:
        with self._lock:
            if self.state in {CourseState.PAUSED, CourseState.STOPPED, CourseState.COMPLETED}:
                return
            old_state = self.state
            self.state = CourseState.PAUSED
            self.history.append((old_state, CourseState.PAUSED))

    def stop(self) -> None:
        with self._lock:
            if self.state == CourseState.STOPPED:
                return
            old_state = self.state
            self.state = CourseState.STOPPED
            self.history.append((old_state, CourseState.STOPPED))


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    attempts: int
    reason: str
    page_state: dict[str, Any] | None = None


@dataclass(frozen=True)
class PageTask:
    """页面中一个可独立验收的内容单元。"""

    kind: str
    state: str
    count: int = 0
    types: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "state": self.state, "count": self.count}
        if self.types:
            value["types"] = self.types
        return value


@dataclass(frozen=True)
class PagePlan:
    """由真实快照构建的页面任务计划，而不是单一页面类型。"""

    tasks: tuple[PageTask, ...]
    active_kind: str
    ready_for_navigation: bool

    @classmethod
    def from_status(cls, status: dict[str, Any], machine: CourseStateMachine | None = None) -> "PagePlan":
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        if status.get("networkDialogPending"):
            return cls((PageTask("network", "pending", 1),), "network", False)
        if status.get("quizLoading"):
            return cls((PageTask("quiz", "pending", 0),), "quiz", False)
        dialog = status.get("dialogState")
        if isinstance(dialog, dict) and dialog.get("policy") != "navigation":
            return cls((PageTask("dialog", "pending", 1),), "dialog", False)
        slides = status.get("slideDocuments") or []
        slides_pending = any(item.get('error') or not item.get("completed") for item in slides)
        slides_error = any(item.get('error') for item in slides)
        # 可见 PPT 有独立页码任务，不能用课程目录勾选跳过尚未翻阅的张数。
        if bool(state.get("recordComplete")) and not status.get("quizStartPending") and not slides_pending:
            return cls((PageTask("record", "completed", 1),), "navigation", True)

        tasks: list[PageTask] = []
        videos = [item for item in status.get("videos", []) if isinstance(item, dict)]
        if videos:
            finished = sum(1 for item in videos if item.get("ended"))
            tasks.append(PageTask("video", "completed" if finished == len(videos) else "pending", len(videos)))

        has_document = bool(slides or status.get("hasDocument") or status.get("hasDocumentFrame"))
        document_done = bool(
            machine
            and machine.content_kind == "document"
            and machine.state in {CourseState.WAITING_PAGE_CONFIRM, CourseState.READY_FOR_NEXT, CourseState.NAVIGATING}
        )
        if slides:
            document_done = not slides_pending
        if has_document:
            tasks.append(PageTask("document", "error" if slides_error else "completed" if document_done else "pending", 1))

        quiz_count = max(0, int(status.get("quizUnfinished") or 0))
        if status.get("quizStartPending"):
            quiz_count = max(1, quiz_count)
        raw_types = status.get("quizTypes") if isinstance(status.get("quizTypes"), dict) else {}
        quiz_types = {str(name): int(count) for name, count in raw_types.items() if int(count or 0) > 0}
        skip_quiz = bool(status.get('quizSkipBeforeStart') and status.get('quizStartPending'))
        if quiz_count:
            tasks.append(PageTask("quiz", "skipped" if skip_quiz else "pending", quiz_count, quiz_types or None))

        # 测验出现时先处理，避免视频和文档动作干扰可交互题目；其余按视频、文档顺序。
        active = next((task.kind for task in tasks if task.kind == "quiz" and task.state not in {"completed", "skipped"}), "")
        if not active:
            active = next((task.kind for task in tasks if task.state not in {"completed", "skipped"}), "navigation")
        return cls(tuple(tasks), active, active == "navigation")

    def as_dict(self) -> list[dict[str, Any]]:
        return [task.as_dict() for task in self.tasks]


class ActionExecutor:
    """统一执行 CDP 输入动作，并用页面指纹验证结果。"""

    def __init__(
        self,
        cdp_call: Callable[..., dict | None],
        evaluate: Callable[[str, float], Any],
        *,
        is_running: Callable[[], bool] = lambda: True,
        sleep: Callable[[float], None] = time.sleep,
        natural_interactions: bool = False,
    ) -> None:
        self._cdp_call = cdp_call
        self._evaluate = evaluate
        self._is_running = is_running
        self._sleep = sleep
        self._natural_interactions = natural_interactions
        self._pointer_position: tuple[float, float] | None = None
        self._action_lock = threading.Lock()

    def set_natural_interactions(self, enabled: bool) -> None:
        """切换有限的真实动作节奏；不生成与任务无关的输入。"""
        self._natural_interactions = bool(enabled)
        self._pointer_position = None

    def _move_pointer(self, x: float, y: float) -> bool:
        if not self._natural_interactions:
            return self._cdp_call(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=5.0,
            ) is not None

        # 目标坐标由页面命中测试提供。这里只在中心附近加入极小偏移，避免越出
        # 小按钮；移动过程使用缓入缓出轨迹，而不是瞬间跳到目标点。
        target_x = max(0.0, x + random.uniform(-2.5, 2.5))
        target_y = max(0.0, y + random.uniform(-2.5, 2.5))
        nearby_hit = self._evaluate(
            "(function(x,y,nx,ny){var a=document.elementFromPoint(x,y);"
            "var b=document.elementFromPoint(nx,ny);return !!(a&&b&&"
            "(a===b||a.contains(b)||b.contains(a)));})"
            f"({json.dumps(x)},{json.dumps(y)},{json.dumps(target_x)},{json.dumps(target_y)})",
            2.0,
        )
        if nearby_hit is not True:
            target_x, target_y = x, y
        start_x, start_y = self._pointer_position or (
            max(0.0, target_x + random.uniform(-80.0, 80.0)),
            max(0.0, target_y + random.uniform(-55.0, 55.0)),
        )
        steps = random.randint(4, 8)
        curve = random.uniform(-5.0, 5.0)
        for index in range(1, steps + 1):
            if not self._is_running():
                return False
            progress = index / steps
            eased = progress * progress * (3.0 - 2.0 * progress)
            bend = math.sin(math.pi * progress) * curve
            move_x = max(0.0, start_x + (target_x - start_x) * eased + bend)
            move_y = max(0.0, start_y + (target_y - start_y) * eased - bend * 0.35)
            if self._cdp_call(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": move_x, "y": move_y}, timeout=5.0,
            ) is None:
                return False
            if index < steps:
                self._sleep(random.uniform(0.012, 0.032))
        self._pointer_position = (target_x, target_y)
        return True

    def click_viewport_point(self, x: float, y: float) -> bool:
        if not self._is_running() or not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
            return False
        if not self._move_pointer(x, y):
            return False
        click_x, click_y = self._pointer_position if self._natural_interactions and self._pointer_position else (x, y)
        if self._natural_interactions:
            self._sleep(random.uniform(0.025, 0.080))
        if self._cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": click_x, "y": click_y, "button": "left", "clickCount": 1},
            timeout=5.0,
        ) is None:
            return False
        if self._natural_interactions:
            self._sleep(random.uniform(0.045, 0.125))
        if self._cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": click_x, "y": click_y, "button": "left", "clickCount": 1},
            timeout=5.0,
        ) is None:
            return False
        if self._natural_interactions:
            self._sleep(random.uniform(0.080, 0.220))
        return True

    def execute_click(self, x: float, y: float) -> bool:
        """带互斥保护的单点点击，供测验作答等独立动作与导航互斥。"""
        if not self._action_lock.acquire(blocking=False):
            return False
        try:
            return self.click_viewport_point(x, y)
        finally:
            self._action_lock.release()

    def dispatch_mouse_wheel(self, x: float, y: float, delta_y: float) -> bool:
        if not self._is_running():
            return False
        params = {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta_y}
        return self._cdp_call("Input.dispatchMouseEvent", params, timeout=5.0) is not None

    def dispatch_key(self, key: str, code: str | None = None) -> bool:
        """封装真实键盘输入；课程控制流程目前不会自动调用。"""
        if not self._is_running() or not key:
            return False
        common = {"key": key, "code": code or key}
        if self._cdp_call("Input.dispatchKeyEvent", {"type": "keyDown", **common}, timeout=5.0) is None:
            return False
        return self._cdp_call("Input.dispatchKeyEvent", {"type": "keyUp", **common}, timeout=5.0) is not None

    def insert_text(self, text: str) -> bool:
        """封装文字输入；课程控制流程目前不会自动调用。"""
        if not self._is_running():
            return False
        return self._cdp_call("Input.insertText", {"text": text}, timeout=5.0) is not None

    def navigation_target(self) -> dict[str, Any] | None:
        value = self._evaluate(
            "window.__yxy_controller && window.__yxy_controller.get_navigation_target()",
            5.0,
        )
        return value if isinstance(value, dict) else None

    def page_state(self) -> dict[str, Any] | None:
        value = self._evaluate(
            "window.__yxy_controller && window.__yxy_controller.get_page_state()",
            5.0,
        )
        return value if isinstance(value, dict) else None

    @staticmethod
    def _page_changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
        if not before or not after:
            return False
        if before.get("page") and after.get("page") and before.get("page") != after.get("page"):
            return True
        if before.get("pageIndex") and after.get("pageIndex") and before.get("pageIndex") != after.get("pageIndex"):
            return True
        # 名称变化必须同时伴随内容/层级变化，避免短暂 DOM 文案刷新被误判为换页。
        name_changed = bool(before.get("pageName") and after.get("pageName") and before.get("pageName") != after.get("pageName"))
        content_changed = any(before.get(key) != after.get(key) for key in ("section", "contentType", "source"))
        return name_changed and content_changed

    @staticmethod
    def _valid_target(target: dict[str, Any]) -> bool:
        try:
            return (
                not target.get("disabled")
                and bool(target.get("pointMatches"))
                and float(target.get("width", 0)) > 0
                and float(target.get("height", 0)) > 0
                and math.isfinite(float(target["x"]))
                and math.isfinite(float(target["y"]))
            )
        except (KeyError, TypeError, ValueError):
            return False

    def execute_navigation(
        self,
        *,
        max_retries: int = 2,
        verify_timeout: float = 8.0,
        poll_interval: float = 0.2,
    ) -> ActionResult:
        if not self._is_running():
            return ActionResult(False, 0, "controller-stopped")
        if not self._action_lock.acquire(blocking=False):
            return ActionResult(False, 0, "action-already-running")
        try:
            before = self.page_state()
            attempts = 0
            for _ in range(max(1, max_retries)):
                if not self._is_running():
                    return ActionResult(False, attempts, "controller-stopped")
                target = self.navigation_target()
                if not target or not self._valid_target(target):
                    return ActionResult(False, attempts, "navigation-target-unavailable")
                attempts += 1
                # CDP Input 会直接投递给已连接的课件标签页；不要激活标签页，
                # 以免刷课过程抢走用户当前正在使用的页面。
                if not self.click_viewport_point(float(target["x"]), float(target["y"])):
                    continue
                deadline = time.monotonic() + max(0.0, verify_timeout)
                while True:
                    after = self.page_state()
                    if self._page_changed(before, after):
                        return ActionResult(True, attempts, "page-state-changed", after)
                    follow_up = self.navigation_target()
                    if (
                        follow_up
                        and follow_up.get("kind") in {"forward-dialog", "completion-dialog"}
                        and self._valid_target(follow_up)
                    ):
                        self.click_viewport_point(float(follow_up["x"]), float(follow_up["y"]))
                        self._sleep(0.4)
                    if not self._is_running():
                        return ActionResult(False, attempts, "controller-stopped", after)
                    if time.monotonic() >= deadline:
                        break
                    self._sleep(min(max(0.01, poll_interval), max(0.01, deadline - time.monotonic())))
            return ActionResult(False, attempts, "postcondition-timeout", self.page_state())
        finally:
            self._action_lock.release()


@dataclass(frozen=True)
class TemplateMatch:
    """第二阶段模板定位接口的数据结构；首轮不依赖 OpenCV。"""

    x: float
    y: float
    width: float
    height: float
    confidence: float


def template_match_to_css(
    match: TemplateMatch,
    *,
    screenshot_size: tuple[int, int],
    viewport_size: tuple[int, int],
    threshold: float = 0.8,
) -> tuple[float, float] | None:
    """把通过阈值的截图矩形中心换算为视口 CSS 坐标。"""

    image_width, image_height = screenshot_size
    viewport_width, viewport_height = viewport_size
    if (
        match.confidence < threshold
        or image_width <= 0
        or image_height <= 0
        or viewport_width <= 0
        or viewport_height <= 0
        or match.width <= 0
        or match.height <= 0
    ):
        return None
    center_x = match.x + match.width / 2
    center_y = match.y + match.height / 2
    return (
        center_x * viewport_width / image_width,
        center_y * viewport_height / image_height,
    )


class CourseController:
    """一个实例控制一个课件标签页；停止时不关闭用户标签页。"""

    def __init__(
        self,
        emit: Callable[[str, str], None],
        emit_event: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.emit = emit
        self._event_emitter = emit_event
        self.ws_url: str | None = None
        self.ws = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._responses: dict[int, Any] = {}
        self._running = False
        self._stop_event = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._document_scroll_thread: threading.Thread | None = None
        self._anti_idle_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._document_scrolled_frames: set[str] = set()
        self._document_completed_frames: set[str] = set()
        self._document_status: dict[str, str] = {}
        self._document_not_scrollable_counts: dict[str, int] = {}
        self._last_frame_urls: list[str] = []
        self._last_target_urls: list[str] = []
        self._iframe_sessions: dict[str, str] = {}
        self._active_config: CourseConfig | None = None
        self._quiz_busy = False
        self._quiz_gate_lock = threading.Lock()
        self._quiz_start_attempts: set[str] = set()
        self._dialog_attempts: dict[tuple[str, str], int] = {}
        self._dialog_warnings: set[tuple[str, str]] = set()
        self._slide_progress: dict[tuple[str, str], dict] = {}
        self._slide_attempts: dict[tuple[str, str, int], int] = {}
        self._agent_answer_provider = None
        self._ai_answer_provider = None
        self._connection_lost = threading.Event()
        self._last_status: dict[str, Any] | None = None
        self._last_progress_signature = ""
        self._last_progress_at = time.monotonic()
        self._watchdog_recoveries = 0
        self._session_token = ""
        self._session_id = ""
        self._session_started_at: float | None = None
        self._observed_page_id = ""
        self._last_progress_wall_time = ""
        self._stall_active = False
        self._stall_reason = ""
        self._stall_page_key = ""
        self._last_recovery_event_attempt = 0
        self._status_read_failures = 0
        self._page_entered_key = ""
        self._video_started_key = ""
        self._content_detected_keys: set[str] = set()
        self._course_completed_emitted = False
        self.state_machine = CourseStateMachine()
        self.action_executor = ActionExecutor(
            self._cdp_call,
            self.eval_js,
            is_running=lambda: self._running,
            natural_interactions=True,
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remain = divmod(total, 3600)
        minutes, secs = divmod(remain, 60)
        prefix = f"{hours:02d}时" if hours else ""
        return f"{prefix}{minutes:02d}分{secs:02d}秒"

    def _session_elapsed_seconds(self) -> int:
        if self._session_started_at is None:
            return 0
        return max(0, int(time.monotonic() - self._session_started_at))

    @staticmethod
    def _page_info_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
        value = state if isinstance(state, dict) else {}
        return {
            "id": str(value.get("page") or ""),
            "name": str(value.get("pageName") or ""),
            "index": int(value.get("pageIndex") or 0),
            "total": int(value.get("pageTotal") or 0),
        }

    def _emit_event(
        self,
        code: str,
        level: str,
        category: str,
        message: str,
        *,
        state: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._event_emitter is None:
            self.emit(message, {"warning": "warn"}.get(level, level))
            return
        self._event_emitter(
            code,
            level,
            category,
            message,
            session_id=self._session_id,
            page=self._page_info_from_state(state),
            data=data or {},
        )

    @staticmethod
    def _page_label(state: dict[str, Any] | None) -> str:
        page = CourseController._page_info_from_state(state)
        suffix = f"（{page['index']}/{page['total']}）" if page["index"] and page["total"] else ""
        return f"{page['name'] or '未知页面'}{suffix}"

    @staticmethod
    def _safe_url(value: str) -> str:
        """诊断日志只保留 URL 的源和路径，不带查询参数或片段。"""
        try:
            parts = urlsplit(str(value or ""))
        except ValueError:
            return ""
        if not parts.scheme:
            return str(value or "").split("?", 1)[0].split("#", 1)[0]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def find_course_tab(self, port: int = 9222) -> str | None:
        try:
            targets = requests.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
        except (requests.RequestException, ValueError):
            return None
        for target in targets:
            if target.get("type") == "page" and COURSE_TAB_URL_KEYWORD in target.get("url", ""):
                return target.get("webSocketDebuggerUrl")
        return None

    def attach(self) -> bool:
        if not self.ws_url:
            return False
        try:
            self.ws = create_connection(self.ws_url, timeout=10, enable_multithread=True)
        except Exception as error:
            self.emit(f"[刷课] 连接 CDP 失败：{error}", "warn")
            return False
        self._running = True
        self._stop_event.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, name="course-cdp-recv", daemon=True)
        self._recv_thread.start()
        self._event_thread = threading.Thread(target=self._event_loop, name="course-event-worker", daemon=True)
        self._event_thread.start()
        self._send("Runtime.enable")
        self._send("Page.enable")
        # 仅在 CDP 中模拟焦点/活动页，不会调用 Page.bringToFront 或切换
        # 用户正在看的标签页；某些课件控件会拒绝非活动页面的输入事件。
        self._send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        return True

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _send(self, method: str, params: dict | None = None, *, session_id: str | None = None) -> int:
        if self.ws is None:
            raise RuntimeError("CDP 尚未连接")
        msg_id = self._next_id()
        message: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        with self._send_lock:
            self.ws.send(json.dumps(message))
        return msg_id

    def _cdp_call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 10.0,
        *,
        session_id: str | None = None,
    ) -> dict | None:
        try:
            msg_id = self._send(method, params, session_id=session_id)
        except Exception:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if msg_id in self._responses:
                    return self._responses.pop(msg_id)
            time.sleep(0.02)
        return None

    def _cdp_eval(
        self,
        expression: str,
        timeout: float = 10.0,
        *,
        context_id: int | None = None,
        session_id: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {
            "expression": expression,
            "awaitPromise": False,
            "returnByValue": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        return self._cdp_call("Runtime.evaluate", params, timeout, session_id=session_id)

    def eval_js(self, expression: str, timeout: float = 10.0):
        result = self._cdp_eval(expression, timeout=timeout)
        if result is None:
            return None
        try:
            return result["result"]["value"]
        except (KeyError, TypeError):
            return None

    def _recv_loop(self) -> None:
        try:
            while self._running:
                try:
                    raw = self.ws.recv()
                    if not raw:
                        continue
                    message = json.loads(raw)
                except Exception:
                    break
                if "id" in message:
                    with self._lock:
                        self._responses[message["id"]] = message.get("result")
                    continue
                method = message.get("method", "")
                if method == "Runtime.consoleAPICalled":
                    self._on_console(message.get("params", {}))
                elif method == "Target.attachedToTarget":
                    self._on_target_attached(message.get("params", {}))
        finally:
            if self._running:
                self._connection_lost.set()

    def _on_console(self, params: dict) -> None:
        parts = []
        for arg in params.get("args", []):
            if arg.get("type") == "string":
                parts.append(arg.get("value", ""))
            elif arg.get("value") is not None:
                parts.append(str(arg["value"]))
        text = " ".join(parts)
        if text.startswith(EVENT_PREFIX):
            try:
                event = json.loads(text[len(EVENT_PREFIX) :].strip())
            except (TypeError, ValueError):
                self.emit("[刷课] 页面返回了无效的结构化事件，已忽略。", "warn")
                return
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                if event.get("session") == self._session_token:
                    self._enqueue_course_event(event)
        elif self._session_token and text.startswith(f"[yxy:{self._session_token}]"):
            self.emit(text.replace(f"[yxy:{self._session_token}]", "[yxy]", 1), "info")

    def _on_target_attached(self, params: dict) -> None:
        info = params.get("targetInfo", {})
        target_id = info.get("targetId")
        session_id = params.get("sessionId")
        if target_id and session_id and info.get("type") == "iframe":
            self._iframe_sessions[target_id] = session_id

    def _enqueue_course_event(self, event: dict[str, Any]) -> None:
        if self._running:
            self._event_queue.put(event)

    def _event_loop(self) -> None:
        while self._running or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                break
            if not self._running:
                continue
            try:
                self._handle_course_event(event)
            except Exception as error:
                self.state_machine.fail()
                self.emit(f"[刷课] 处理页面事件失败：{error}", "warn")

    @staticmethod
    def _state_key(state: dict[str, Any] | None) -> str:
        if not state:
            return ""
        return json.dumps(
            {key: state.get(key, "") for key in ("url", "chapter", "section", "page", "pageName", "source")},
            ensure_ascii=False,
            sort_keys=True,
        )

    def _handle_course_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        event_page = str(event.get("page") or "")
        if event_type in {"page-ready", "chapter-changed"}:
            state = event.get("state")
            if isinstance(state, dict) and state.get("page"):
                incoming_page = str(state["page"])
                if event_type == "page-ready" and self._observed_page_id and incoming_page != self._observed_page_id:
                    self.emit(f"[刷课] 已忽略旧页面 {incoming_page} 的迟到 page-ready 事件。", "muted")
                    return
                self._observed_page_id = incoming_page
        elif event_page:
            if self._observed_page_id and event_page != self._observed_page_id:
                self.emit(
                    f"[刷课] 已忽略旧页面 {event_page} 的迟到事件 {event_type}。",
                    "muted",
                )
                return
            if not self._observed_page_id:
                self._observed_page_id = event_page
        if event_type == "page-ready":
            state = event.get("state") if isinstance(event.get("state"), dict) else {}
            key = self._state_key(state)
            if key and key != self._page_entered_key:
                self._page_entered_key = key
                self._emit_event(
                    "PAGE_ENTERED", "info", "navigation",
                    f"已进入：{self._page_label(state)}", state=state,
                )
            return
        if event_type == "video-ready":
            source = str(event.get("source", ""))
            if self.state_machine.observe_video_ready(source):
                content_key = f"{event_page or source}:video"
                if content_key not in self._content_detected_keys:
                    self._content_detected_keys.add(content_key)
                    state = (self._last_status or {}).get("state")
                    self._emit_event(
                        "CONTENT_DETECTED", "info", "content",
                        f"检测到视频：{int(event.get('total') or 1)} 个", state=state,
                        data={"kind": "video", "count": int(event.get("total") or 1)},
                    )
            return
        if event_type == "video-playing":
            source = str(event.get("source", ""))
            started = self.state_machine.observe_video_playing(source)
            if started and source != self._video_started_key:
                self._video_started_key = source
                self._emit_event(
                    "VIDEO_STARTED", "info", "video", "视频开始播放",
                    state=(self._last_status or {}).get("state"),
                )
            return
        if event_type == "document-reading":
            key = str(event.get("frame") or event.get("url") or "")
            if self.state_machine.observe_document_reading(key):
                self._emit_event(
                    "CONTENT_DETECTED", "info", "content", "检测到文档",
                    state=(self._last_status or {}).get("state"), data={"kind": "document"},
                )
            return
        if event_type == "static-ready":
            if any(item.get('error') or not item.get('completed') for item in self.status_snapshot().get('slideDocuments', [])):
                return
            key = str(event.get("source", ""))
            if event.get('quizSkipped'):
                current = self.status_snapshot()
                if (current.get('readOk') is False or not current.get('quizSkipBeforeStart')
                        or not current.get('quizStartPending') or not self._navigation_precondition_met(current)):
                    return
                if self.state_machine.observe_static_ready(key) and self.state_machine.mark_content_finished('static', key):
                    self._emit_event('QUIZ_SKIPPED', 'info', 'quiz', '自动答题已关闭：跳过未开始的限时测验，不启动计时',
                                     state=current.get('state'), data={'reason': 'auto-answer-disabled'})
                    self._attempt_navigation_if_ready()
                return
            if event.get("recordComplete") and self.state_machine.mark_record_complete(key):
                state = (self._last_status or {}).get("state")
                self._emit_event(
                    "PAGE_COMPLETED", "success", "content",
                    f"本页完成：{self._page_info_from_state(state)['name'] or '当前页面'}，准备翻页",
                    state=state, data={"kind": "record"},
                )
                self._attempt_navigation_if_ready()
            elif self.state_machine.observe_static_ready(key) and self.state_machine.mark_content_finished("static", key):
                state = (self._last_status or {}).get("state")
                self._emit_event(
                    "PAGE_COMPLETED", "success", "content",
                    f"本页完成：{self._page_info_from_state(state)['name'] or '当前页面'}，准备翻页",
                    state=state, data={"kind": "static"},
                )
                self._attempt_navigation_if_ready()
            return
        if event_type in {"video-ended", "document-bottom"}:
            kind = "video" if event_type == "video-ended" else "document"
            key = str(event.get("source") if kind == "video" else (event.get("frame") or event.get("url") or ""))
            status = self.status_snapshot()
            if any(item.get('error') for item in status.get('slideDocuments', [])):
                return
            if event_type == 'document-bottom' and any(not item.get('completed') for item in status.get('slideDocuments', [])):
                return
            # 已完成记录优先于播放器的 0:00/ended 外观，避免把已看完页面
            # 误报成“视频刚播放结束”。
            if kind == "video" and bool((status.get("state") or {}).get("recordComplete")):
                if self.state_machine.mark_record_complete(key):
                    state = status.get("state")
                    self._emit_event(
                        "PAGE_COMPLETED", "success", "content",
                        f"本页完成：{self._page_info_from_state(state)['name'] or '当前页面'}，准备翻页",
                        state=state, data={"kind": "record"},
                    )
                    self._attempt_navigation_if_ready()
                return
            # 一页可同时有视频和文档。视频完成后先让文档任务接管，不能把
            # “视频 ended”误当成整个页面完成。
            if kind == "video":
                plan = self._page_plan(status)
                if plan.active_kind == "document" and self.state_machine.prepare_document_after_video():
                    return
            if self.state_machine.mark_content_finished(kind, key):
                state = (self._last_status or {}).get("state")
                if kind == "video":
                    total = int(event.get("total") or 1)
                    message = f"本页完成：视频 {total}/{total}，准备翻页"
                else:
                    message = f"本页完成：{self._page_info_from_state(state)['name'] or '文档'}，准备翻页"
                self._emit_event(
                    "PAGE_COMPLETED", "success", "content", message, state=state,
                    data={"kind": kind, "completed": int(event.get("total") or 1)},
                )
                self._attempt_navigation_if_ready()
            return
        if event_type == "next-ready":
            self._attempt_navigation_if_ready()
            return
        if event_type == "chapter-changed":
            state = event.get("state")
            self.state_machine.page_changed(self._state_key(state if isinstance(state, dict) else None))
            self._reset_document_state()
            return
        if event_type == "video-stalled":
            attempt = int(event.get("recovery") or 1)
            self._begin_recovery("video", attempt, (self._last_status or {}).get("state"))
            return
        if event_type == "quiz-appeared":
            self._on_quiz_appeared(event)
            return
        if event_type == "quiz-finished":
            if self._stall_active:
                self._finish_recovery((self._last_status or {}).get("state"))
            return
        if event_type == "course-finished":
            status = self.status_snapshot()
            if not status.get("courseFinished") or not self._navigation_precondition_met(status):
                return
            if self._quiz_busy and self._active_config and self._active_config.quiz_mode in {"agent", "ai"}:
                return
            target = self.action_executor.navigation_target()
            if target and target.get("kind") == "forward-dialog":
                self.action_executor.execute_click(float(target.get("x", -1)), float(target.get("y", -1)))
            if self.state_machine.complete():
                self._emit_course_completed(event.get("state") if isinstance(event.get("state"), dict) else None)
            return
        if event_type == "video-error":
            self.emit(f"[刷课] 视频自动恢复仍未推进：{event.get('reason', '未知原因')}，看门狗将继续处理。", "warn")

    def _quiz_busy_now(self) -> bool:
        """防走神动作前检查是否有未完成测验，避免干扰作答坐标。"""
        if self._quiz_busy:
            return True
        value = self.eval_js(
            "(function(){var c=window.__yxy_controller;var s=c&&c.get_status&&c.get_status();"
            "if(s&&(s.quizStartPending||s.dialogState))return true;var v=document.querySelector('.question-view');"
            "return !!(v && v.getBoundingClientRect().width > 0"
            " && document.querySelectorAll('.question-wrapper:not(.finished)').length > 0);})()"
        )
        return bool(value)

    def _anti_idle_loop(self, interval: float) -> None:
        """定期滚轮下滑再上滑（真实 wheel 事件），重置平台挂机计时。"""
        self.emit("[刷课] 防走神滚动已启动。", "info")
        while self._running and not self._stop_event.is_set():
            self._stop_event.wait(max(5.0, interval * random.uniform(0.8, 1.2)))
            if not self._running or self._quiz_busy_now():
                continue
            for delta in (240, -240):
                if not self._running:
                    break
                self._cdp_call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": 640, "y": 400, "deltaX": 0, "deltaY": delta},
                    timeout=5.0,
                )
                time.sleep(random.uniform(0.4, 0.9))

    def _read_page_status(self) -> dict[str, Any] | None:
        value = self.eval_js("window.__yxy_controller && window.__yxy_controller.get_status()", timeout=5.0)
        return dict(value) if isinstance(value, dict) else None

    def _read_bootstrap_state(self) -> dict[str, Any] | None:
        """注入前只读当前课程/页面名称，保证会话事件先于页面事件。"""
        value = self.eval_js(
            "(function(){try{var r=window.koLearnCourseViewModel;"
            "var o=function(v){return typeof v==='function'?v():v};"
            "var c=r&&o(r.course),p=r&&o(r.currentPage);return {"
            "courseName:String(c&&o(c.name)||''),page:String(p&&o(p.id)||''),"
            "pageName:String(p&&o(p.name)||'')};}catch(e){return null;}})()",
            timeout=5.0,
        )
        return dict(value) if isinstance(value, dict) else None

    def _return_to_course_start(self, current: dict[str, Any] | None) -> dict[str, Any] | None:
        """启动会话前回到课程第一张可见页面，并验证 pageId 真实变化。"""
        target = self.eval_js(FIRST_PAGE_TARGET_JS, timeout=5.0)
        if not isinstance(target, dict) or not target.get("page"):
            return None
        expected_page = str(target["page"])
        if target.get("alreadyCurrent"):
            return current or self._read_bootstrap_state()
        try:
            x, y = float(target["x"]), float(target["y"])
        except (KeyError, TypeError, ValueError):
            return None
        for _attempt in range(2):
            if not self.action_executor.execute_click(x, y):
                continue
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                state = self._read_bootstrap_state()
                if state and str(state.get("page") or "") == expected_page:
                    return state
                self._stop_event.wait(0.2)
            target = self.eval_js(FIRST_PAGE_TARGET_JS, timeout=5.0)
            if not isinstance(target, dict) or target.get("alreadyCurrent"):
                break
            try:
                x, y = float(target["x"]), float(target["y"])
            except (KeyError, TypeError, ValueError):
                break
        return None

    def status_snapshot(self) -> dict[str, Any]:
        """返回稳定的前端快照；读取失败时保留最后一次真实页面数据并明确标记。"""
        current = self._read_page_status() if self._running and not self._connection_lost.is_set() else None
        if current is not None:
            self._last_status = current
            self._status_read_failures = 0
        elif self._running:
            self._status_read_failures += 1
        snapshot = dict(current or self._last_status or {})
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
        active_video = next((item for item in videos if isinstance(item, dict) and not item.get("ended")), None)
        if active_video is None:
            active_video = next((item for item in reversed(videos) if isinstance(item, dict)), {})
        plan = self._page_plan(snapshot) if state.get("page") else PagePlan((), "waiting", False)
        task_names = {"video": "视频", "document": "文档", "quiz": "测验", "network": "网络提示", "dialog": "页面提示", "navigation": "翻页", "record": "翻页"}
        completed = self.state_machine.state == CourseState.COMPLETED or self._course_completed_emitted
        task = "完成" if completed else "恢复" if self._stall_active else task_names.get(plan.active_kind, "等待")
        resource_error = next((item for item in snapshot.get('slideDocuments', []) if item.get('error')), None)
        if resource_error:
            task = '课件错误（等待资源恢复）'
        page_completed, completion_source = self._completion_status(snapshot) if state.get("page") else (False, "")
        page_info = self._page_info_from_state(state)
        page_info["completed"] = page_completed
        snapshot.update({
            "sessionId": self._session_id,
            "running": self._running,
            "completed": completed,
            "paused": self.state_machine.state in {CourseState.PAUSED, CourseState.ERROR},
            "controllerState": self.state_machine.state.value,
            "connected": bool(self.ws is not None and not self._connection_lost.is_set()),
            "readOk": current is not None if self._running else True,
            "readFailures": self._status_read_failures,
            "courseName": str(state.get("courseName") or ""),
            "page": page_info,
            "pageCompleted": page_completed,
            "completionSource": completion_source,
            "currentTask": task,
            "resourceError": ({**resource_error['error'], 'current': resource_error.get('current', 0),
                               'total': resource_error.get('total', 0)} if resource_error else None),
            "pagePlan": plan.as_dict(),
            "video": {
                "currentTime": float(active_video.get("currentTime") or 0),
                "duration": float(active_video.get("duration") or 0),
                "rate": float(active_video.get("rate") or (self._active_config.playback_rate if self._active_config else 1)),
            },
            "playbackRate": float(active_video.get("rate") or (self._active_config.playback_rate if self._active_config else 1)),
            "lastProgressTime": self._last_progress_wall_time,
            "retryCount": self._watchdog_recoveries,
            "maxRetries": 3,
            "stalled": self._stall_active,
            "stallReason": self._stall_reason,
        })
        return snapshot

    def _completion_status(self, status: dict[str, Any]) -> tuple[bool, str]:
        if status.get('quizSkipBeforeStart') and status.get('quizStartPending'):
            return False, ''
        plan = self._page_plan(status)
        if not plan.ready_for_navigation:
            return False, ""
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        if bool(state.get("recordComplete")):
            return True, "record"
        if self.state_machine.content_kind == "document":
            return True, "document"
        videos = status.get("videos") if isinstance(status.get("videos"), list) else []
        if videos:
            return True, "video"
        return True, "static"

    def _page_plan(self, status: dict[str, Any] | None) -> PagePlan:
        return PagePlan.from_status(status if isinstance(status, dict) else {}, self.state_machine)

    def _navigation_precondition_met(self, status: dict[str, Any]) -> bool:
        return self._page_plan(status).ready_for_navigation

    def _emit_course_completed(self, state: dict[str, Any] | None) -> None:
        if self._course_completed_emitted:
            return
        self._course_completed_emitted = True
        page = self._page_info_from_state(state or (self._last_status or {}).get("state"))
        done = page["total"] or page["index"]
        elapsed_seconds = self._session_elapsed_seconds()
        elapsed_text = self._format_elapsed(elapsed_seconds)
        self._emit_event(
            "COURSE_COMPLETED", "success", "session",
            f"课程完成：{done}/{page['total'] or done}，用时 {elapsed_text}", state=state,
            data={"completed": done, "total": page["total"] or done, "elapsedSeconds": elapsed_seconds, "elapsed": elapsed_text},
        )
        # 完成是终止态，不再让看门狗把最后一页的静止媒体误判为停滞。
        self._stall_active = False
        self._stall_reason = ""
        self._watchdog_recoveries = 0
        self._stop_event.set()
        try:
            self._cdp_eval("window.__yxy_controller && window.__yxy_controller.cleanup()", timeout=3.0)
        except Exception:
            pass
        self._running = False
        ws = self.ws
        self.ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _begin_recovery(self, reason: str, attempt: int, state: dict[str, Any] | None = None) -> None:
        if self.state_machine.state == CourseState.COMPLETED or self._course_completed_emitted:
            return
        attempt = max(1, min(3, attempt))
        if not self._stall_active:
            self._stall_active = True
            self._stall_reason = reason
            self._stall_page_key = self._state_key(state)
            message = "视频未推进" if reason == "video" else "翻页未生效" if reason == "navigation" else "页面状态未推进"
            self._emit_event(
                "STALL_DETECTED", "warning", "recovery",
                f"{message}，正在恢复（{attempt}/3）", state=state,
                data={"reason": reason, "attempt": attempt, "maxAttempts": 3},
            )
        self._watchdog_recoveries = max(self._watchdog_recoveries, attempt)
        if attempt <= self._last_recovery_event_attempt:
            return
        self._last_recovery_event_attempt = attempt
        self._emit_event(
            "RECOVERY_STARTED", "warning", "recovery",
            f"开始恢复（{attempt}/3）", state=state,
            data={"reason": reason, "attempt": attempt, "maxAttempts": 3},
        )

    def _finish_recovery(self, state: dict[str, Any] | None = None) -> None:
        if not self._stall_active:
            return
        reason = self._stall_reason
        self._stall_active = False
        self._stall_reason = ""
        self._stall_page_key = ""
        self._watchdog_recoveries = 0
        self._last_recovery_event_attempt = 0
        self._emit_event(
            "RECOVERY_SUCCEEDED", "success", "recovery",
            "播放已恢复" if reason == "video" else "运行已恢复",
            state=state, data={"reason": reason},
        )

    def _progress_resolves_recovery(self, status: dict[str, Any]) -> bool:
        if any(item.get('error') for item in status.get('slideDocuments', [])):
            return False
        if not self._stall_active:
            return True
        if self._stall_reason != "navigation":
            return True
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        return self._state_key(state) != self._stall_page_key

    def _pause_after_recovery_failure(self) -> None:
        """重试耗尽后停止所有自动动作，同时保留 CDP 连接供前端读取最终状态。"""
        self.state_machine.pause()
        self._stop_event.set()
        self.eval_js(
            "(function(){if(window.__yxy_controller)window.__yxy_controller.cleanup();"
            "document.querySelectorAll('video').forEach(function(v){v.pause();});return true;})()",
            timeout=5.0,
        )

    @staticmethod
    def _progress_signature(status: dict[str, Any]) -> str:
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        videos = []
        for video in status.get("videos", []) if isinstance(status.get("videos"), list) else []:
            if isinstance(video, dict):
                videos.append(
                    {
                        "index": video.get("index"),
                        "time": int(float(video.get("currentTime") or 0)),
                        "ended": bool(video.get("ended")),
                        "ready": video.get("readyState"),
                    }
                )
        return json.dumps(
            {
                "page": state.get("page"),
                "pageName": state.get("pageName"),
                "recordComplete": state.get("recordComplete"),
                "quiz": status.get("quizUnfinished"),
                "dialog": status.get("dialog"),
                "videos": videos,
                "slides": status.get("slideDocuments"),
                "courseFinished": status.get("courseFinished"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _reconnect_cdp(self) -> bool:
        ws_url = self.find_course_tab()
        if not ws_url:
            return False
        try:
            replacement = create_connection(ws_url, timeout=10, enable_multithread=True)
        except Exception:
            return False
        old_ws = self.ws
        self._session_token = uuid.uuid4().hex
        self.ws_url = ws_url
        self.ws = replacement
        with self._lock:
            self._responses.clear()
        try:
            if old_ws is not None:
                old_ws.close()
        except Exception:
            pass
        self._connection_lost.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, name="course-cdp-recv", daemon=True)
        self._recv_thread.start()
        self._send("Runtime.enable")
        self._send("Page.enable")
        self._send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        config = self._active_config
        return bool(config and self.inject_main_script(config))

    def _recover_stall(self, status: dict[str, Any]) -> None:
        if any(item.get('error') for item in status.get('slideDocuments', [])):
            return  # 平台资源错误不能靠反复点击/恢复播放器解决。
        if self._quiz_busy and self._active_config and self._active_config.quiz_mode in {"agent", "ai"}:
            return
        if self.state_machine.state == CourseState.COMPLETED or self._course_completed_emitted:
            return
        state = status.get("state") if isinstance(status.get("state"), dict) else {}
        skip_quiz = status.get('quizSkipBeforeStart') and status.get('quizStartPending')
        if not skip_quiz and (status.get("quizStartPending") or int(status.get("quizUnfinished") or 0) > 0):
            self._on_quiz_appeared({"unfinished": status.get("quizUnfinished")})
            return
        target = self.action_executor.navigation_target()
        if status.get("dialog") and target:
            self.action_executor.execute_click(float(target.get("x", -1)), float(target.get("y", -1)))
            return
        self.eval_js("window.__yxy_controller && window.__yxy_controller.recover()", timeout=5.0)
        videos = status.get("videos") if isinstance(status.get("videos"), list) else []
        if videos and all(isinstance(video, dict) and video.get("ended") for video in videos):
            key = f"page-{state.get('page', '')}"
            if self.state_machine.state == CourseState.LOADING:
                self.state_machine.observe_video_ready(key)
                self.state_machine.observe_video_playing(key)
            self.state_machine.mark_content_finished("video", key)
            if self.state_machine.state == CourseState.WAITING_PAGE_CONFIRM:
                self._attempt_navigation_if_ready(recovery=True)
        elif self.state_machine.state == CourseState.WAITING_PAGE_CONFIRM:
            self._attempt_navigation_if_ready(recovery=True)

    def _handle_classified_dialog(self) -> bool:
        config = self._active_config
        if not self._running or self._quiz_busy or not config or not config.auto_dismiss_dialog:
            return False
        outcome, dialog = handle_dialog(
            self.eval_js, self.action_executor.execute_click, self._stop_event.wait,
            lambda: self._running and not self._stop_event.is_set(), self._dialog_attempts,
        )
        if outcome == "dismissed":
            # 仅证明这一层提示关闭；资源、记录和交卷结果由后续快照核实。
            self._emit_event("DIALOG_DISMISSED", "info", "content", f"已处理：{dialog['title']}",
                             state=(self._last_status or {}).get("state"),
                             data={"kind": dialog['type'], "policy": dialog['policy']})
            return True
        if dialog and outcome not in {"absent", "changed", "stopped"}:
            key = (dialog.get("signature", ""), outcome)
            if key not in self._dialog_warnings:
                self._dialog_warnings.add(key)
                reason = {"blocked": "暂无已验证动作", "unavailable": "按钮不可用或被遮挡",
                          "exhausted": "已达本页三次尝试上限", "click-failed": "点击失败",
                          "unverified": "点击后尚未确认关闭"}.get(outcome, outcome)
                self._emit_event("DIALOG_UNRESOLVED", "warn", "content",
                                 f"{dialog['title']}：{reason}", state=(self._last_status or {}).get("state"),
                                 data={"kind": dialog['type'], "buttons": dialog['buttons'], "reason": reason})
                self.emit(f"[刷课] {dialog['title']} ({dialog['type']})：{reason}；按钮：{' / '.join(dialog['buttons'])}", "warn")
        return False

    def _handle_network_dialog(self) -> bool:
        """兼容原入口，所有提示统一使用共享策略与重试预算。"""
        return self._handle_classified_dialog()

    def _watchdog_loop(self) -> None:
        self.emit("[刷课] 页面状态与停滞看门狗已启动。", "info")
        connection_warning = False
        connection_attempts = 0
        while self._running and not self._stop_event.wait(5.0):
            if self.state_machine.state == CourseState.COMPLETED or self._course_completed_emitted:
                return
            if self._quiz_busy and self._active_config and self._active_config.quiz_mode in {"agent", "ai"}:
                # Waiting for external input or AI generation is intentional, not stalled media.
                self._last_progress_at = time.monotonic()
                if (self._active_config.quiz_mode == "agent" and self._connection_lost.is_set()
                        and self._agent_answer_provider):
                    provider = self._agent_answer_provider
                    provider.manager.fail_task(provider.task_id, "QUIZ_PAGE_CHANGED", "The course connection was lost.")
                continue
            if self._connection_lost.is_set():
                if not connection_warning:
                    self.emit("[刷课] 已检测到课件页 CDP 连接中断，正在自动重连。", "warn")
                    connection_warning = True
                connection_attempts += 1
                self._begin_recovery("cdp", connection_attempts, (self._last_status or {}).get("state"))
                if self._reconnect_cdp():
                    self.emit("[刷课] 课件页 CDP 已重连并恢复控制。", "success")
                    self._last_progress_at = time.monotonic()
                    self._finish_recovery((self._last_status or {}).get("state"))
                    connection_warning = False
                    connection_attempts = 0
                elif connection_attempts >= 3:
                    self._pause_after_recovery_failure()
                    self._emit_event(
                        "RECOVERY_FAILED", "error", "recovery", "CDP 重连失败：已暂停（3/3）",
                        state=(self._last_status or {}).get("state"),
                        data={"reason": "cdp", "attempt": 3, "maxAttempts": 3},
                    )
                    return
                continue
            status = self.status_snapshot()
            if not status.get("readOk"):
                if int(status.get("readFailures") or 0) >= 3:
                    if self._watchdog_recoveries >= 3:
                        self._pause_after_recovery_failure()
                        self._emit_event(
                            "RECOVERY_FAILED", "error", "recovery", "页面状态持续读取失败：已暂停（3/3）",
                            state=status.get("state"), data={"reason": "status-read", "attempt": 3, "maxAttempts": 3},
                        )
                        return
                    if not self._stall_active or time.monotonic() - self._last_progress_at >= 25.0:
                        attempt = self._watchdog_recoveries + 1
                        self._begin_recovery("status-read", attempt, status.get("state"))
                        self._last_progress_at = time.monotonic()
                        self.inject_main_script(self._active_config) if self._active_config else None
                continue
            self._last_status = status
            if any(item.get('error') for item in status.get('slideDocuments', [])):
                self._last_progress_at = time.monotonic()
                continue  # 保留文档只读检测，不消耗通用恢复重试，也不误报恢复成功。
            dialog = status.get("dialogState")
            if isinstance(dialog, dict) and dialog.get("policy") != "navigation":
                self._handle_classified_dialog()
                continue
            if status.get("networkDialogPending"):
                self._handle_network_dialog()
                continue
            if status.get("courseFinished"):
                self._enqueue_course_event({"type": "course-finished", "state": status.get("state")})
            signature = self._progress_signature(status)
            if signature != self._last_progress_signature:
                self._last_progress_signature = signature
                state = status.get("state") if isinstance(status.get("state"), dict) else {}
                if self._progress_resolves_recovery(status):
                    self._last_progress_at = time.monotonic()
                    self._last_progress_wall_time = self._now_iso()
                    self._finish_recovery(state)
                continue
            stalled_for = time.monotonic() - self._last_progress_at
            retry_delay = 35.0 if self._stall_active and self._stall_reason == "navigation" else 25.0
            if stalled_for < retry_delay:
                continue
            if self._watchdog_recoveries >= 3:
                self._pause_after_recovery_failure()
                self._emit_event(
                    "RECOVERY_FAILED", "error", "recovery",
                    "恢复失败：已暂停（3/3）", state=status.get("state"),
                    data={"reason": self._stall_reason or "no-progress", "attempt": 3, "maxAttempts": 3},
                )
                return
            attempt = self._watchdog_recoveries + 1
            reason = "navigation" if self.state_machine.state in {CourseState.WAITING_PAGE_CONFIRM, CourseState.READY_FOR_NEXT, CourseState.NAVIGATING} else "video" if status.get("videos") else "no-progress"
            self._begin_recovery(reason, attempt, status.get("state"))
            self._last_progress_at = time.monotonic()
            self._recover_stall(status)

    def _on_quiz_appeared(self, event: dict[str, Any]) -> None:
        config = self._active_config
        if config is None or not config.quiz_auto_answer or config.quiz_mode == 'disabled':
            self.emit("[刷课] 检测到未完成测验（自动作答未开启），请人工完成。", "muted")
            return
        with self._quiz_gate_lock:
            if self._quiz_busy:
                return
            self._quiz_busy = True
        count = int(event.get("unfinished") or 0)
        self._emit_event(
            "QUIZ_DETECTED", "info", "quiz", f"检测到测验：{count} 题",
            state=(self._last_status or {}).get("state"), data={"count": count},
        )
        threading.Thread(target=self._run_quiz_handler, name="course-quiz", daemon=True).start()

    def _run_quiz_handler(self) -> None:
        config = self._active_config
        quiz_session = self._session_id
        try:
            if not config or not config.quiz_auto_answer or config.quiz_mode == 'disabled':
                return
            if config and config.quiz_mode in {"agent", "ai"}:
                provider = self._agent_answer_provider if config.quiz_mode == "agent" else self._ai_answer_provider
                if provider is None:
                    self.stop()
                    return
                self.eval_js("window.__yxy_agent_waiting = true")
                handler = QuizHandler(
                    evaluate=self.eval_js, click=self.action_executor.execute_click,
                    type_text=self.action_executor.insert_text, is_running=lambda: self._running,
                    log=lambda *_args: None, dry_run=False, jitter=0,
                    start_attempts=self._quiz_start_attempts,
                    dialog_attempts=self._dialog_attempts,
                    auto_dismiss_dialog=config.auto_dismiss_dialog,
                )
                result = provider.answer(handler, self) if config.quiz_mode == "agent" else provider.answer(handler, self, config)
                if result["state"] == "fallback":
                    self.emit(f"[刷课] AI 答题失败，改用固定答案：{result['reason']}。", "warn")
                else:
                    if result["state"] != "completed":
                        if self._session_id == quiz_session:
                            self.stop()
                    else:
                        self._last_progress_at = time.monotonic()
                        completed = int((result.get("result") or {}).get("completedCount") or 0)
                        self._emit_event(
                            "QUIZ_SUBMITTED", "success", "quiz",
                            f"AI 测验已提交：完成 {completed} 题",
                            state=(self._last_status or {}).get("state"),
                            data={"completed": completed, "skipped": 0, "failed": 0},
                        )
                    return
            fixed_attempts = 1 if config and config.quiz_mode == "ai" else 2
            for attempt in range(fixed_attempts):
                handler = QuizHandler(
                    evaluate=self.eval_js,
                    click=self.action_executor.execute_click,
                    type_text=self.action_executor.insert_text,
                    is_running=lambda: self._running,
                    log=lambda text, kind="info": self.emit(text, kind),
                    dry_run=False,
                    start_attempts=self._quiz_start_attempts,
                    dialog_attempts=self._dialog_attempts,
                    auto_dismiss_dialog=config.auto_dismiss_dialog if config else True,
                )
                summary = handler.answer_all(
                    option_label=config.quiz_option_label if config else "C",
                    judgment_label=config.quiz_judgment_label if config else "错误",
                    blank_text=config.quiz_blank_text if config else ",",
                    answer_choice=config.quiz_choice_enabled if config else True,
                    answer_judgment=config.quiz_judgment_enabled if config else True,
                    answer_blank=config.quiz_blank_enabled if config else True,
                )
                if not self._running:
                    return
                self.emit(
                    f"[刷课] 测验处理结束：作答 {summary['done']}，跳过 {summary['skipped']}，"
                    f"失败 {summary['failed']}，处理弹窗 {summary['modals']} 次。",
                    "success" if summary["failed"] == 0 else "warn",
                )
                if summary["done"] or summary["skipped"] or summary["failed"]:
                    self._emit_event(
                        "QUIZ_SUBMITTED", "success" if summary["failed"] == 0 else "warning", "quiz",
                        f"测验已提交：完成 {summary['done']}，跳过 {summary['skipped']}",
                        state=(self._last_status or {}).get("state"),
                        data={"completed": summary["done"], "skipped": summary["skipped"], "failed": summary["failed"]},
                    )
                if summary["failed"] == 0:
                    break
                if config and config.quiz_mode == "ai":
                    self.emit("[刷课] 固定答案降级执行失败，已停止课程任务，请人工检查当前页。", "warn")
                    if self._session_id == quiz_session:
                        self.stop()
                    return
                if attempt == 0:
                    self.emit("[刷课] 存在失败题目，30 秒后自动重试一轮。", "warn")
                    self._stop_event.wait(30.0)
        except Exception as error:
            if config and config.quiz_mode in {"agent", "ai"}:
                label = "Agent" if config.quiz_mode == "agent" else "AI"
                self.emit(f"[刷课] {label} 测验处理失败，已停止课程任务。", "warn")
                if self._session_id == quiz_session:
                    self.stop()
            else:
                self.emit(f"[刷课] 测验自动作答异常停止：{error}", "warn")
        finally:
            if self._session_id == quiz_session:
                if config and config.quiz_mode in {"agent", "ai"} and self._running:
                    self.eval_js("window.__yxy_agent_waiting = false")
                self._quiz_busy = False

    def _attempt_navigation_if_ready(self, *, recovery: bool = False) -> None:
        if self._quiz_busy or not self._running or self.state_machine.state != CourseState.WAITING_PAGE_CONFIRM:
            return
        # 页面可能在首轮失败后补发 next-ready/static-ready 等迟到事件；
        # 停滞期间只允许看门狗按退避间隔发起下一次点击。
        if self._stall_active and not recovery:
            return
        status = self.status_snapshot()
        if status and status.get("courseFinished") and self._navigation_precondition_met(status):
            if self.state_machine.complete():
                self._emit_course_completed(status.get("state"))
            return
        if not self._navigation_precondition_met(status):
            self.emit("[刷课] 页面完成状态尚未确认，本轮不执行翻页。", "muted")
            return
        target = self.action_executor.navigation_target()
        if not target:
            return
        if not self.state_machine.mark_next_ready() or not self.state_machine.begin_navigation():
            return
        state = status.get("state") if status else (self._last_status or {}).get("state")
        self._emit_event(
            "NAVIGATION_STARTED", "info", "navigation",
            f"开始翻页：{self._page_info_from_state(state)['name'] or '当前页面'}", state=state,
        )
        self._perform_navigation()

    def _perform_navigation(self) -> None:
        started_at = time.monotonic()
        # 页面明确完成后仍留出一个短阅读/反应窗口；等待可被停止操作立即打断。
        if self._stop_event.wait(random.uniform(1.4, 3.8)) or not self._running:
            return
        # 每个恢复周期只点击一次；由外层看门狗做带间隔的有限重试，避免连续点按。
        result = self.action_executor.execute_navigation(max_retries=1, verify_timeout=10.0)
        if result.ok:
            self.state_machine.navigation_succeeded(self._state_key(result.page_state))
            if result.page_state and result.page_state.get("page"):
                self._observed_page_id = str(result.page_state["page"])
            self._reset_document_state()
            self._last_progress_at = time.monotonic()
            self._last_progress_wall_time = self._now_iso()
            self._finish_recovery(result.page_state)
            self._emit_event(
                "PAGE_CHANGED", "success", "navigation",
                f"翻页成功：{self._page_label(result.page_state)}", state=result.page_state,
                data={"attempts": result.attempts, "elapsedMs": round((time.monotonic() - started_at) * 1000)},
            )
            return
        if result.reason == "controller-stopped":
            return
        if result.reason == "navigation-target-unavailable":
            status = self.status_snapshot()
            if status and status.get("courseFinished") and self._navigation_precondition_met(status):
                if self.state_machine.complete():
                    self._emit_course_completed(status.get("state"))
                return
        self.state_machine.navigation_failed()
        attempt = min(3, self._watchdog_recoveries + 1)
        self._begin_recovery("navigation", attempt, (result.page_state or (self._last_status or {}).get("state")))
        self._last_progress_at = time.monotonic()

    @staticmethod
    def _walk_frames(frame_tree: dict) -> list[dict]:
        frames: list[dict] = []
        if not isinstance(frame_tree, dict):
            return frames
        frame = frame_tree.get("frame")
        if isinstance(frame, dict):
            frames.append(frame)
        for child in frame_tree.get("childFrames", []) or []:
            frames.extend(CourseController._walk_frames(child))
        return frames

    def _document_frames(self) -> list[dict]:
        result = self._cdp_call("Page.getFrameTree", timeout=5.0)
        if not result:
            self._last_frame_urls = []
            return []
        all_frames = self._walk_frames(result.get("frameTree", {}))
        self._last_frame_urls = [self._safe_url(frame.get("url", "")) for frame in all_frames]
        return [frame for frame in all_frames if "ulearning.cn" in frame.get("url", "")]

    def _document_log_once(self, key: str, text: str, kind: str = "info") -> None:
        if self._document_status.get(key) == text or (key == "no-document-frame" and key in self._document_status):
            return
        self._document_status[key] = text
        self.emit(text, kind)

    def _frame_context_id(self, frame_id: str) -> int | None:
        result = self._cdp_call(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": "yxy-course-document-scroll", "grantUniveralAccess": True},
            timeout=5.0,
        )
        try:
            return int(result["executionContextId"])
        except (KeyError, TypeError, ValueError):
            return None

    def _document_targets(self) -> list[dict]:
        result = self._cdp_call("Target.getTargets", timeout=5.0)
        if not result:
            self._last_target_urls = []
            return []
        infos = result.get("targetInfos", [])
        self._last_target_urls = [
            f"{info.get('type', '?')}:{self._safe_url(info.get('url', ''))}" for info in infos
        ]
        documents = [
            info for info in infos
            if info.get("type") == "iframe" and "ulearning.cn" in info.get("url", "")
        ]
        attached: list[dict] = []
        for info in documents:
            target_id = info.get("targetId")
            if not target_id:
                continue
            owner = self._cdp_call("DOM.getFrameOwner", {"frameId": target_id}, timeout=5.0)
            if not owner or not owner.get("backendNodeId"):
                continue  # Target.getTargets 也包含其他标签页的 iframe。
            session_id = self._iframe_sessions.get(target_id)
            if not session_id:
                response = self._cdp_call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                    timeout=5.0,
                )
                session_id = (response or {}).get("sessionId")
                if session_id:
                    self._iframe_sessions[target_id] = session_id
            if session_id:
                attached.append(
                    {"id": target_id, "url": info.get("url", ""), "sessionId": session_id, "kind": "oopif"}
                )
            else:
                self._document_log_once(
                    f"attach-target:{target_id}",
                    f"[刷课] 已发现文档 OOPIF，但无法附着：{self._safe_url(info.get('url', ''))}",
                    "warn",
                )
        return attached

    _FRAME_SCROLL_JS = r"""
(() => {
  /* SLIDE_READER */
  const slides = readSlideDocument();
  if (slides) return slides;
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 80 && rect.height > 80 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const sidebar = element => /sidebar|catalog|outline|menu|toc|directory|left-nav|chapter-list|catalogue/i
    .test(String(element.className || '') + ' ' + String(element.id || ''));
  const candidates = [document.scrollingElement, document.documentElement, document.body]
    .concat(Array.from(document.querySelectorAll('*')))
    .filter(element => element && visible(element) && !sidebar(element) &&
      element.scrollHeight > element.clientHeight + 2)
    .sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight));
  const target = candidates[0];
  if (!target) return {state: 'not-scrollable', url: location.href};
  const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
  const remaining = maxTop - target.scrollTop;
  if (remaining <= 2) return {state: 'complete', url: location.href, target: target.tagName};
  const oldTop = target.scrollTop;
  const step = Math.max(160, Math.min(560, Math.round(target.clientHeight * 0.65)));
  target.scrollTop = Math.min(oldTop + step, maxTop);
  return {
    state: target.scrollTop > oldTop ? 'scrolled' : 'unchanged',
    url: location.href,
    top: target.scrollTop,
    remaining: remaining,
    target: target.tagName
  };
})()
"""

    _FRAME_SCROLL_JS = _FRAME_SCROLL_JS.replace('/* SLIDE_READER */', SLIDE_READER_JS)

    def _scroll_document_frame(self, frame: dict) -> dict | None:
        frame_id = frame.get("id")
        if not frame_id:
            return None
        context_id = self._frame_context_id(frame_id)
        if context_id is None:
            self._document_log_once(
                f"context:{frame_id}",
                f"[刷课] 文档 frame 无法创建执行环境：{self._safe_url(frame.get('url', ''))}",
                "warn",
            )
            return None
        result = self._cdp_eval(self._FRAME_SCROLL_JS, timeout=5.0, context_id=context_id)
        try:
            value = result["result"]["value"]
            if isinstance(value, dict) and str(value.get("state", "")).startswith("slides"):
                return self._advance_slide_document(frame, value, context_id=context_id)
            return value if isinstance(value, dict) else None
        except (KeyError, TypeError):
            self._document_log_once(
                f"eval:{frame_id}",
                f"[刷课] 文档 frame 执行滚动脚本失败：{self._safe_url(frame.get('url', ''))}",
                "warn",
            )
            return None

    def _scroll_document_target(self, target: dict) -> dict | None:
        result = self._cdp_eval(
            self._FRAME_SCROLL_JS,
            timeout=5.0,
            session_id=target.get("sessionId"),
        )
        try:
            value = result["result"]["value"]
            if isinstance(value, dict) and str(value.get("state", "")).startswith("slides"):
                return self._advance_slide_document(target, value)
            return value if isinstance(value, dict) else None
        except (KeyError, TypeError):
            self._document_log_once(
                f"oopif-eval:{target.get('id', '')}",
                f"[刷课] 文档 OOPIF 执行滚动脚本失败：{self._safe_url(target.get('url', ''))}",
                "warn",
            )
            return None

    def _slide_target_point(self, item: dict, state: dict) -> tuple[float, float] | None:
        owner = self._cdp_call('DOM.getFrameOwner', {'frameId': item['id']}, timeout=5.0)
        if not owner or not owner.get('backendNodeId'):
            return None
        box = self._cdp_call('DOM.getBoxModel', {'backendNodeId': owner['backendNodeId']}, timeout=5.0)
        point = frame_point((box or {}).get('model', {}).get('content'), state)
        if point is None:
            return None
        resolved = self._cdp_call('DOM.resolveNode', {'backendNodeId': owner['backendNodeId']}, timeout=5.0)
        object_id = (resolved or {}).get('object', {}).get('objectId')
        if not object_id:
            return None
        try:
            checked = self._cdp_call('Runtime.callFunctionOn', {
                'objectId': object_id, 'returnByValue': True,
                'functionDeclaration': """function(x,y,resource) {
                  let hash=2166136261; const src=this.src || '';
                  for(let i=0;i<src.length;i++) hash=Math.imul(hash^src.charCodeAt(i),16777619);
                  return this.isConnected && this.tagName==='IFRAME' && (hash>>>0).toString(16)===resource &&
                    x>=0 && y>=0 && x<innerWidth && y<innerHeight && document.elementFromPoint(x,y)===this;
                }""",
                'arguments': [{'value': point[0]}, {'value': point[1]}, {'value': state['resource']}],
            }, timeout=5.0)
            return point if (checked or {}).get('result', {}).get('value') is True else None
        finally:
            self._cdp_call('Runtime.releaseObject', {'objectId': object_id}, timeout=5.0)

    def _advance_slide_document(self, item: dict, state: dict, *, context_id=None) -> dict:
        page = str((self._read_bootstrap_state() or {}).get('page') or '')
        resource = state.get('resource', '')
        key = (page, resource)
        previous = self._slide_progress.get(key)

        def publish(observed, completed=False):
            old_error = (self._slide_progress.get(key) or {}).get('error')
            progress = {name: observed.get(name, 0) for name in ('resource', 'current', 'total')}
            progress.update(page=page, completed=completed, ready=observed.get('state') == 'slides')
            if observed.get('error'):
                progress['error'] = observed['error']
            self._slide_progress[key] = progress
            self.eval_js('window.__yxy_slide_progress=window.__yxy_slide_progress||{};'
                         'window.__yxy_slide_progress[' + json.dumps(resource) + ']=' + json.dumps(progress))
            error = progress.get('error')
            if error:
                self._stall_active = False
                self._stall_reason = ''
                self._watchdog_recoveries = 0
                self._last_recovery_event_attempt = 0
                if error != old_error:
                    position = f"（第 {progress['current']}/{progress['total']} 张）" if progress['total'] else ''
                    self._emit_event('RESOURCE_ERROR', 'error', 'content',
                                     error['message'] + position + '，已停止翻页并等待资源恢复；不会自动刷新或跳过。',
                                     state={'page': page}, data={'source': 'course-player', **error,
                                                              'current': progress['current'], 'total': progress['total']})
            elif old_error:
                self._emit_event('RESOURCE_ERROR_CLEARED', 'info', 'content',
                                 '课件播放器错误提示已消失，继续检查资源；尚未判定完成。', state={'page': page})

        def read():
            result = self._cdp_eval(SLIDE_STATE_JS, timeout=5.0, context_id=context_id, session_id=item.get('sessionId'))
            return (result or {}).get('result', {}).get('value')

        if not page or not resource or not self._running or self._quiz_busy:
            return {**state, 'state': 'slides-wait'}
        publish(state)
        if state.get('state') != 'slides':
            return {**state, 'page': page}
        current, total = state['current'], state['total']
        if not previous or not previous.get('ready') or previous.get('current') != current or previous.get('total') != total:
            return {**state, 'state': 'slides-reading', 'page': page}
        if current == total:
            publish(state, completed=True)
            return {**state, 'state': 'slides-complete', 'page': page}
        attempt_key = (page, resource, current)
        if self._slide_attempts.get(attempt_key, 0) >= 3:
            self._document_log_once(f'slides:{attempt_key}', f'[刷课] PPT 第 {current}/{total} 张翻页未生效，已停止重复点击。', 'warn')
            return {**state, 'state': 'slides-wait'}
        fresh = read()
        if isinstance(fresh, dict) and fresh.get('resource') == resource and fresh.get('state') == 'slides-error':
            publish(fresh)
            return {**fresh, 'page': page}
        if not isinstance(fresh, dict) or any(fresh.get(name) != state.get(name) for name in ('resource', 'current', 'total', 'state')):
            return {**state, 'state': 'slides-wait'}
        if not fresh.get('target') or self._quiz_busy_now():
            return {**state, 'state': 'slides-wait'}
        point = self._slide_target_point(item, fresh)
        if point is None or not self._running or self._quiz_busy or str((self._read_bootstrap_state() or {}).get('page') or '') != page:
            return {**state, 'state': 'slides-wait'}
        self._slide_attempts[attempt_key] = self._slide_attempts.get(attempt_key, 0) + 1
        if not self.action_executor.execute_click(*point):
            return {**state, 'state': 'slides-wait'}
        for _ in range(80):
            if self._stop_event.wait(0.05) or not self._running:
                break
            after = read()
            if not isinstance(after, dict) or after.get('resource') != resource or after.get('total') != total:
                break
            if str((self._read_bootstrap_state() or {}).get('page') or '') != page:
                break
            if after.get('state') == 'slides-error':
                publish(after)
                return {**after, 'page': page}
            if after.get('state') == 'slides' and after.get('current') == current + 1:
                publish(after)
                self._last_progress_at = time.monotonic()
                self._last_progress_wall_time = self._now_iso()
                self.emit(f'[刷课] PPT 已翻至第 {current + 1}/{total} 张。', 'info')
                return {**after, 'state': 'slides-reading', 'page': page}
            if after.get('current') not in (current, current + 1):
                break
        return {**state, 'state': 'slides-wait'}

    def _handle_document_scroll_state(self, item: dict, state: dict) -> None:
        item_id = item.get("id", "")
        if str(state.get('state', '')).startswith('slides'):
            if state.get('state') == 'slides-error':
                self._document_completed_frames.discard(item_id)
                return
            if state.get('state') == 'slides-reading':
                self._document_completed_frames.discard(item_id)
                self._document_scrolled_frames.add(item_id)
                self._enqueue_course_event({'type': 'document-reading', 'frame': item_id, 'page': state.get('page')})
            elif state.get('state') == 'slides-complete' and item_id not in self._document_completed_frames:
                self._document_completed_frames.add(item_id)
                self.emit(f"[刷课] PPT 已到末张（{state['total']}/{state['total']}），等待课程页面确认。", 'info')
                self._enqueue_course_event({'type': 'document-bottom', 'frame': item_id, 'page': state.get('page')})
            return
        if state.get("state") == "scrolled":
            first_scroll = item_id not in self._document_scrolled_frames
            self._document_scrolled_frames.add(item_id)
            self._document_completed_frames.discard(item_id)
            self._document_log_once(
                f"scrolling:{item_id}",
                f"[刷课] 正在滚动文档：{self._safe_url(state.get('url', item.get('url', '')))}",
            )
            if first_scroll:
                self._enqueue_course_event(
                    {"type": "document-reading", "frame": item_id, "url": state.get("url", item.get("url", ""))}
                )
        elif state.get("state") == "complete" and item_id in self._document_scrolled_frames:
            if item_id not in self._document_completed_frames:
                self._document_completed_frames.add(item_id)
                self.emit("[刷课] 文档已滚动至末尾，等待页面确认。", "info")
                self._enqueue_course_event(
                    {"type": "document-bottom", "frame": item_id, "url": state.get("url", item.get("url", ""))}
                )
        elif state.get("state") == "not-scrollable":
            count = self._document_not_scrollable_counts.get(item_id, 0) + 1
            self._document_not_scrollable_counts[item_id] = count
            self._document_log_once(
                f"not-scrollable:{item_id}",
                f"[刷课] 文档无需滚动或尚在加载：{self._safe_url(state.get('url', item.get('url', '')))}",
                "muted",
            )
            if count >= 3 and item_id not in self._document_completed_frames:
                self._document_scrolled_frames.add(item_id)
                self._document_completed_frames.add(item_id)
                self._enqueue_course_event(
                    {"type": "document-reading", "frame": item_id, "url": state.get("url", item.get("url", ""))}
                )
                self._enqueue_course_event(
                    {"type": "document-bottom", "frame": item_id, "url": state.get("url", item.get("url", ""))}
                )

    @staticmethod
    def _document_poll_delay(interval: float, states: list[dict]) -> float:
        # PPT 已就绪/刚成功翻页时快速继续；加载、失败和普通文档保留原节奏。
        if states and all(state.get('state') == 'slides-reading' for state in states):
            return 0.15
        return max(1.0, interval)

    def _document_scroll_loop(self, interval: float) -> None:
        self.emit("[刷课] 跨域文档滚动器已启动。", "info")
        while self._running and not self._stop_event.is_set():
            if self._quiz_busy:
                self._stop_event.wait(max(1.0, interval))
                continue
            # 文档是页面计划中的后续单元时，等视频/测验实际完成再滚动，避免
            # 两类内容并发造成“看起来完成、实际未验收”的竞态。
            plan = self._page_plan(self.status_snapshot())
            if plan.active_kind not in {"document", "navigation"}:
                self._stop_event.wait(max(1.0, interval))
                continue
            try:
                targets = self._document_targets()
                frames = self._document_frames() if not targets else []
            except Exception as error:
                self._document_log_once("frame-tree-error", f"[刷课] 读取文档 frame 失败：{error}", "warn")
                self._stop_event.wait(max(1.0, interval))
                continue
            items = targets or frames
            document_states: list[dict] = []
            active_ids = {item.get("id", "") for item in items}
            stale_ids = (self._document_scrolled_frames | self._document_completed_frames) - active_ids
            self._document_scrolled_frames.difference_update(stale_ids)
            self._document_completed_frames.difference_update(stale_ids)
            if not items:
                self._document_log_once(
                    "no-document-frame",
                    "[刷课] 当前页无跨域文档，继续按视频/测验流程处理。",
                    "muted",
                )
            for target in targets:
                if self._quiz_busy:
                    break
                target_id = target.get("id", "")
                self._document_log_once(
                    f"found-oopif:{target_id}",
                    f"[刷课] 已发现独立文档 OOPIF：{self._safe_url(target.get('url', ''))}",
                )
                state = self._scroll_document_target(target)
                document_states.append(state or {})
                if state:
                    self._handle_document_scroll_state(target, state)
            for frame in frames:
                if self._quiz_busy:
                    break
                frame_id = frame.get("id", "")
                self._document_log_once(
                    f"found:{frame_id}",
                    f"[刷课] 已发现文档 frame：{self._safe_url(frame.get('url', ''))}",
                )
                state = self._scroll_document_frame(frame)
                document_states.append(state or {})
                if state:
                    self._handle_document_scroll_state(frame, state)
            self._stop_event.wait(self._document_poll_delay(interval, document_states))

    def _reset_document_state(self) -> None:
        self._slide_progress.clear()
        self._slide_attempts.clear()
        self._document_scrolled_frames.clear()
        self._document_completed_frames.clear()
        self._document_status.clear()
        self._document_not_scrollable_counts.clear()
        self._last_frame_urls.clear()
        self._last_target_urls.clear()

    def inject_main_script(self, config: CourseConfig) -> bool:
        values = asdict(config)
        values["session_token"] = self._session_token
        script = f"window.__YXY_CONFIG__={json.dumps(values, ensure_ascii=False)};\n{INJECT_JS}"
        result = self._cdp_eval(script, timeout=15.0)
        if result is None:
            self.emit("[刷课] 注入 JS 超时", "warn")
            return False
        return True

    def start(self, config: CourseConfig) -> bool:
        with self._lifecycle_lock:
            if self._running:
                self.emit("[刷课] 控制器已在运行，无需重复启动。", "warn")
                return False
            try:
                self.state_machine.reset_for_start()
            except ValueError as error:
                self.emit(f"[刷课] 无法启动：{error}", "warn")
                return False
            self._stop_event.clear()
            self._quiz_busy = False
            self._quiz_start_attempts = set()
            self._dialog_attempts.clear()
            self._dialog_warnings.clear()
            self._connection_lost.clear()
            self._active_config = config
            self._session_token = uuid.uuid4().hex
            self._session_id = f"course-{datetime.now().astimezone():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            self._session_started_at = time.monotonic()
            self._last_status = None
            self._observed_page_id = ""
            self._last_progress_signature = ""
            self._last_progress_at = time.monotonic()
            self._watchdog_recoveries = 0
            self._last_recovery_event_attempt = 0
            self._stall_active = False
            self._stall_reason = ""
            self._stall_page_key = ""
            self._status_read_failures = 0
            self._page_entered_key = ""
            self._video_started_key = ""
            self._content_detected_keys.clear()
            self._course_completed_emitted = False
            self._last_progress_wall_time = self._now_iso()
            self._reset_document_state()
            self._iframe_sessions.clear()
            while not self._event_queue.empty():
                try:
                    self._event_queue.get_nowait()
                except queue.Empty:
                    break
            self.ws_url = self.find_course_tab()
            if not self.ws_url:
                self.state_machine.fail()
                self.emit("[刷课] 未找到课件学习页。请先在浏览器打开 ua.dgut.edu.cn 课件学习页。", "warn")
                return False
            self.emit("[刷课] 已定位课件标签页，正在连接 CDP…", "info")
            if not self.attach():
                self.state_machine.fail()
                return False
            self.eval_js("window.__yxy_agent_waiting = false")
            # 连接完成即进入 LOADING，确保注入脚本同步产生的首批媒体事件不会
            # 因仍处于 ATTACHING 而被状态机忽略。
            self.state_machine.transition(CourseState.LOADING)
            bootstrap_state = self._read_bootstrap_state()
            start_state = self._return_to_course_start(bootstrap_state)
            if start_state is None:
                self.state_machine.fail()
                self.emit("[刷课] 无法确认已返回课程开头，未启动刷课。", "warn")
                self.stop()
                return False
            bootstrap_state = start_state
            course_name = str((bootstrap_state or {}).get("courseName") or "当前课程")
            self._emit_event(
                "SESSION_STARTED", "success", "session",
                f"开始刷课：{course_name}（{config.playback_rate:g}×）", state=bootstrap_state,
                data={"playbackRate": config.playback_rate},
            )
            if not self.inject_main_script(config):
                self.state_machine.fail()
                self.stop()
                return False
            initial = self._read_page_status()
            if initial:
                self._last_status = initial
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="course-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            if config.document_scroll_enabled:
                speed = max(1.0, min(3.0, float(config.document_scroll_speed)))
                interval = max(1.0, float(config.document_scroll_interval) / speed)
                self._document_scroll_thread = threading.Thread(
                    target=self._document_scroll_loop,
                    args=(interval,),
                    name="course-document-scroll",
                    daemon=True,
                )
                self._document_scroll_thread.start()
            if config.anti_idle_scroll:
                self._anti_idle_thread = threading.Thread(
                    target=self._anti_idle_loop,
                    args=(max(15.0, float(config.anti_idle_interval)),),
                    name="course-anti-idle",
                    daemon=True,
                )
                self._anti_idle_thread.start()
            return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            was_active = self._running or self.ws is not None
            if self.ws is not None and self._running:
                try:
                    self._cdp_eval("window.__yxy_controller && window.__yxy_controller.cleanup()", timeout=3.0)
                except Exception:
                    pass
            self._running = False
            self._stop_event.set()
            self.state_machine.stop()
            self._event_queue.put(None)
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
            current = threading.current_thread()
            for thread in (
                self._document_scroll_thread,
                self._anti_idle_thread,
                self._watchdog_thread,
                self._event_thread,
                self._recv_thread,
            ):
                if thread and thread is not current and thread.is_alive():
                    thread.join(timeout=2.0)
            self._document_scroll_thread = None
            self._anti_idle_thread = None
            self._watchdog_thread = None
            self._event_thread = None
            self._recv_thread = None
            self._responses.clear()
            self._iframe_sessions.clear()
            self._reset_document_state()
            if was_active:
                elapsed_seconds = self._session_elapsed_seconds()
                elapsed_text = self._format_elapsed(elapsed_seconds)
                self._emit_event(
                    "SESSION_STOPPED", "info", "session", f"刷课已停止，用时 {elapsed_text}",
                    state=(self._last_status or {}).get("state"),
                    data={"reason": "user", "elapsedSeconds": elapsed_seconds, "elapsed": elapsed_text},
                )

    def set_speed(self, rate: float) -> None:
        value = float(rate)
        if not math.isfinite(value) or not 1 <= value <= 16:
            raise ValueError("视频倍速必须在 1 到 16 之间")
        if not self._running:
            self.emit("[刷课] 控制器尚未运行，倍速将在下次启动时生效。", "muted")
            return
        self.eval_js(f"window.__yxy_set_speed({json.dumps(value)})")


__all__ = [
    "INJECT_JS",
    "FIRST_PAGE_TARGET_JS",
    "COURSE_TAB_URL_KEYWORD",
    "ActionExecutor",
    "ActionResult",
    "CourseConfig",
    "CourseController",
    "CourseState",
    "CourseStateMachine",
    "TemplateMatch",
    "template_match_to_css",
]
