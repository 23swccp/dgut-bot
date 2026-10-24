"""用当前浏览器身份单次测试非空签到码；不读登录缓存，不轮询或重试。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dgutbot.domain.sign_monitor import APP_BASE, SignMonitor

try:
    from .browser_session import BrowserSession
except ImportError:
    from browser_session import BrowserSession


def run(port: int, classroom_id: int, attendance_id: int, code: str, location: str, address: str, score_type: int = 2) -> dict:
    if not code.strip():
        raise ValueError("测试码不能为空")
    if score_type not in (1, 2):
        raise ValueError("仅支持二维码或数字码签到实验")
    login = BrowserSession()
    try:
        identity = login.import_browser(port)
        if identity["roleId"] != 9:
            raise ValueError("当前浏览器身份不是学生，未提交")
        user_id = identity["userId"]
        session = login._session
        response = session.get(
            f"{APP_BASE}/wisdomClassroom/student/classroomActivitys",
            params={"classroomId": classroom_id, "pageNum": 1, "pageSize": 999},
            timeout=15, allow_redirects=False,
        )
        response.raise_for_status()
        activities = response.json()
        if activities.get("code") != 1:
            raise ValueError("课堂活动读取未通过，未提交")
        target = next((item for item in activities["result"]["list"]
                       if str(item.get("relationId")) == str(attendance_id)), None)
        if not target or any(str(target.get(key)) != expected for key, expected in (
            ("relationType", "1"), ("scoreType", str(score_type)), ("status", "0"),
        )):
            raise ValueError("目标不是当前课堂进行中的指定类型签到，未提交")
        detail_url = f"https://lms.dgut.edu.cn/apps/newAttendance/getAttendanceForStu/{attendance_id}/{user_id}"

        def detail_status():
            detail = session.get(detail_url, timeout=15, allow_redirects=False)
            detail.raise_for_status()
            return detail.json().get("status")

        before = detail_status()
        if str(before) != "0":
            raise ValueError(f"提交前个人状态为 {before}，未提交")
        payload = {"attendanceID": attendance_id, "classID": classroom_id, "userID": user_id,
                   "location": location, "address": address, "enterWay": 1, "attendanceCode": code}
        # 复用程序原有请求头与传输，只更换非空码；不启动监测线程。
        sender = SignMonitor(request=session.request, selected_course=lambda: None,
                             settings=lambda: SimpleNamespace(poll_interval=5),
                             token=lambda: session.headers["Authorization"], user_id=lambda: user_id,
                             log=lambda *_: None, write_log=lambda *_: None)
        response = sender.direct_sign_request(payload)  # 整个脚本只调用一次，不自动重试。
        result = {"http": response.status_code, "before": before, "response": response.text}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        result["after"] = detail_status()
        print(json.dumps({"after": result["after"]}, ensure_ascii=False), flush=True)
        return result
    finally:
        login.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--classroom-id", type=int, required=True)
    parser.add_argument("--attendance-id", type=int, required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--score-type", type=int, choices=(1, 2), default=2, help="1=二维码，2=数字码")
    parser.add_argument("--location", default="0,0")
    parser.add_argument("--address", default="")
    args = parser.parse_args()
    run(args.port, args.classroom_id, args.attendance_id, args.code, args.location, args.address, args.score_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
