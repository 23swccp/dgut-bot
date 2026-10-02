"""课程/测验共用的只读弹窗策略。新增变体只扩展此表，不泛化点击按钮。"""

import math

DIALOG_POLICY_JS = r"""
function courseDialogState(preferStay = false) {
  const shown = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const text = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const read = value => typeof value === 'function' ? value() : value;
  const root = window.koLearnCourseViewModel;
  const current = root && read(root.currentPage);
  const page = String(current && read(current.id) || '');
  const selectors = '.modal, [role="dialog"], .dialog, .el-message-box, .ant-modal, .layui-layer, .popup, .user-guide';
  const dialogs = Array.from(document.querySelectorAll(selectors)).filter(shown);
  const outer = dialogs.filter(el => !dialogs.some(parent => parent !== el && parent.contains(el)));
  // Multiple stacked dialogs: inspect the top layer first, then re-read after closing it.
  outer.reverse().sort((a, b) => (parseFloat(getComputedStyle(b).zIndex) || 0) - (parseFloat(getComputedStyle(a).zIndex) || 0));
  const dialog = outer[0];
  if (!dialog) return null;
  let type = dialog.id === 'alertModal' ? String(root && read(root.modalType) || 'unknown') : 'unknown';
  const body = text(dialog);
  const rules = {
    docNoWifi: ['network', '非 Wi-Fi 文档提示', /不是\s*wi[-\s]?fi\s*网络.*消耗流量/i, /^继续$/],
    videoNoWifi: ['network', '非 Wi-Fi 视频提示', /不是\s*wi[-\s]?fi\s*网络.*消耗流量/i, /^继续$/],
    suspend: ['resume', '学习计时暂停', /计时.*暂停|走神太久/, /^继续学习$/],
    multiLearning: ['resume', '多页面学习提示', /同时学习多个页面|正在学习其他页面/, /^继续学习$/],
    videoGuide: ['ack', '视频学习说明', /视频.*时长|首次观看视频/, /^(我)?知道了$/],
    splitScreen: ['ack', '分屏作答说明', /退出分屏/, /^(我)?知道了$/],
    autoSubmit: ['ack', '限时练习已自动交卷通知', /时间已耗尽.*自动提交/, /^(我)?知道了$/],
    incompleteTimeLimit: ['ack', '限时题目未完成', /限时答题.*未完成/, /^(我)?知道了$/],
    docFailed: ['ack', '文档加载异常', /文档未显示/, /^(我)?知道了$/],
    flashFailed: ['ack', 'Flash 加载异常', /加载异常/, /^(我)?知道了$/],
    videoFailed: ['ack', '视频加载异常', /加载异常/, /^(我)?知道了$/],
    createRecordFailed: ['retry', '保存学习记录失败', /保存学习记录失败/, /^重试$/],
    goBackCreateRecordFailed: ['stay', '退出前记录未保存', /保存学习记录失败/, /^留在本页$/],
    incomplete: ['navigation', '未完成题目切页确认', /题目.*没有完成/, /^确定离开$/],
    stopLearning: ['blocked', '本页学习已停止', /停止学习/, null],
    createRecordFailedTooMany: ['blocked', '学习记录多次保存失败', /保存学习记录失败/, null],
    timeLimit: ['blocked', '待验证的限时练习入口', /限时练习/, null],
    incompleteOralItem: ['blocked', '口语任务未完成', /未读完/, null]
  };
  let rule = Object.prototype.hasOwnProperty.call(rules, type) ? rules[type] : null;
  if (dialog.matches('.user-guide')) {
    type = 'userGuide';
    rule = ['ack', '新手引导', /跳过所有提示/, /^跳过所有提示$/];
  } else if (dialog.querySelector('.stat-page') && /本章成绩|本节成绩|完成本章|完成本节/.test(body)) {
    type = 'statistics';
    rule = ['navigation', '章节统计', /本章|本节/, /^(继续\s*下\s*一[章节](?:\s*>>)?|关闭)$/];
  }
  const controls = Array.from(dialog.querySelectorAll('button, a, [role="button"], .close-btn')).filter(shown);
  const buttons = controls.map(text).filter(Boolean).slice(0, 8);
  const policy = rule && rule[2].test(body) && !/本人确认|身份验证|验证码|请确认在场/.test(body) ? rule[0] : 'blocked';
  const result = {type, policy, title: rule ? rule[1] : '未知弹窗', page, buttons,
    signature: JSON.stringify([page, type, dialog.id, buttons]), target: null};
  if (policy === 'blocked') return result;
  const pattern = policy === 'navigation' && preferStay ? /^(?:<<\s*)?留在本页$/ : rule[3];
  const matches = controls.filter(el => pattern.test(text(el)) &&
    (type !== 'userGuide' || /\bclick\s*:\s*hideGuide\b/.test(el.getAttribute('data-bind') || '')));
  if (matches.length !== 1) return result;
  const el = matches[0], r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (el.disabled || el.getAttribute('aria-disabled') === 'true' || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return result;
  const hit = document.elementFromPoint(x, y);
  if (!hit || !(hit === el || el.contains(hit))) return result;
  result.target = {kind: 'classified-dialog', x, y, width: r.width, height: r.height,
    viewportWidth: innerWidth, viewportHeight: innerHeight, disabled: false, pointMatches: true, label: text(el)};
  return result;
}
"""

DIALOG_STATE_JS = '(function(){' + DIALOG_POLICY_JS + 'return courseDialogState();})()'
DIALOG_READ_JS = '(function(){' + DIALOG_POLICY_JS + 'return {dialog: courseDialogState()};})()'
DIALOG_READ_STAY_JS = '(function(){' + DIALOG_POLICY_JS + 'return {dialog: courseDialogState(true)};})()'
AUTOMATIC_DIALOG_POLICIES = frozenset({'network', 'resume', 'ack', 'retry', 'stay'})


def handle_dialog(evaluate, click, pause, running, attempts, *, allowed=AUTOMATIC_DIALOG_POLICIES, expected=None, prefer_stay=False):
    """一次只执行一个已分类动作；读取失败与“没有弹窗”明确区分。"""
    raw = evaluate(DIALOG_READ_STAY_JS if prefer_stay else DIALOG_READ_JS)
    dialog = raw.get('dialog') if isinstance(raw, dict) else None
    if not isinstance(dialog, dict):
        return 'absent' if isinstance(raw, dict) and 'dialog' in raw else 'read-failed', {}
    if expected and dialog.get('signature') != expected:
        return 'changed', dialog
    if dialog.get('policy') not in allowed:
        return 'blocked', dialog
    target = dialog.get('target')
    if not isinstance(target, dict) or not target.get('pointMatches') or target.get('disabled'):
        return 'unavailable', dialog
    try:
        x, y = float(target['x']), float(target['y'])
    except (KeyError, TypeError, ValueError):
        return 'unavailable', dialog
    if not all(math.isfinite(value) and value >= 0 for value in (x, y)):
        return 'unavailable', dialog
    if not dialog.get('page') or not running():
        return 'unavailable', dialog
    key = (dialog['page'], dialog['type'])
    if attempts.get(key, 0) >= 3:
        return 'exhausted', dialog
    attempts[key] = attempts.get(key, 0) + 1
    if not click(x, y):
        return 'click-failed', dialog
    for _ in range(10):
        pause(0.2)
        if not running():
            return 'stopped', dialog
        check = evaluate(DIALOG_READ_JS)
        if not isinstance(check, dict) or 'dialog' not in check:
            continue
        after = check['dialog']
        if after is None or isinstance(after, dict) and after.get('signature') != dialog['signature']:
            return 'dismissed', dialog
    return 'unverified', dialog
