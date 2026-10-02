"""优学院 docs PPT 播放器：只读页码/控件识别及坐标换算。"""

import math

SLIDE_READER_JS = r"""
function readSlideDocument() {
  const ppt = document.querySelector('#ppt');
  if (!ppt) return null;
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  let hash = 2166136261;
  for (let i = 0; i < location.href.length; i++) hash = Math.imul(hash ^ location.href.charCodeAt(i), 16777619);
  const state = {state: 'slides-loading', resource: (hash >>> 0).toString(16), current: 0, total: 0,
    viewportWidth: innerWidth, viewportHeight: innerHeight, target: null};
  const index = ppt.querySelector('#bottom #PageIndex'), count = ppt.querySelector('#bottom #PageCount');
  const parse = el => /^\d+$/.test(el.textContent.trim()) ? Number(el.textContent.trim()) : 0;
  state.current = index ? parse(index) : 0; state.total = count ? parse(count) : 0;
  const message = document.querySelector('#msg');
  const text = visible(message) ? message.textContent.trim() : '';
  if (text) {
    let code = 'player-message', description = '课件播放器显示异常提示';
    if (/解析.{0,8}(出错|失败)|转换失败/.test(text)) {
      code = 'parse-failed'; description = '课件播放器报告解析失败（平台端）';
    } else if (/加载失败|网络错误|加载出错/.test(text)) {
      code = 'load-failed'; description = '课件播放器报告加载失败';
    } else if (/试读|无权|权限|禁止访问/.test(text)) {
      code = 'access-limited'; description = '课件播放器提示访问或试读限制';
    }
    return {...state, state: 'slides-error', error: {code, message: description}};
  }
  if (!visible(ppt) || !index || !count) return state;
  if (state.current < 1 || state.total < state.current || !Number.isSafeInteger(state.total)) return state;
  const slide = ppt.querySelector('#view' + (state.current - 1));
  if (!visible(slide) || Array.from(ppt.querySelectorAll('.waitmsg')).some(visible)) return state;
  state.state = 'slides';
  if (state.current === state.total) return state;
  const buttons = ppt.querySelectorAll('#bottom #pageNext.pgRight[title="下一张"]');
  if (buttons.length !== 1) return state;
  const button = buttons[0];
  if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return state;
  const r = button.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return state;
  const hit = document.elementFromPoint(x, y);
  if (!hit || !(hit === button || button.contains(hit))) return state;
  state.target = {x, y, pointMatches: true};
  return state;
}
"""
SLIDE_STATE_JS = '(function(){' + SLIDE_READER_JS + 'return readSlideDocument();})()'


def frame_point(quad, state):
    """iframe CSS 内容框映射到主视口；拒绝旋转、缺失及无效坐标。"""
    try:
        x0, y0, x1, y1, x2, y2, x3, y3 = map(float, quad)
        w, h = float(state['viewportWidth']), float(state['viewportHeight'])
        x, y = float(state['target']['x']), float(state['target']['y'])
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1, x2, y2, x3, y3, w, h, x, y)):
            return None
        if w <= 0 or h <= 0 or not (0 <= x < w and 0 <= y < h):
            return None
        if x1 <= x0 or y3 <= y0 or max(abs(y1-y0), abs(y2-y3), abs(x2-x1), abs(x3-x0)) > 0.5:
            return None
        return x0 + x * (x1-x0) / w, y0 + y * (y3-y0) / h
    except (TypeError, ValueError, KeyError):
        return None
