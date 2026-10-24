"""从已验证的教师调试浏览器取得会话，每 5 秒直接读取服务端数字码并离线分析。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


try:
    from .browser_session import BrowserSession, BrowserSessionError
    from .digit_code_report import analyze
except ImportError:
    from browser_session import BrowserSession, BrowserSessionError
    from digit_code_report import analyze


def write_samples(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_code(session, attendance_id: int) -> tuple[str | None, dict]:
    response = session.get(
        f"https://application.dgut.edu.cn/classroomapi/newAttendance/getNewAttendanceDetail/{attendance_id}/4/1",
        params={"pageSize": 1}, timeout=12, allow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "attendanceCode" not in data or str(data.get("status")).lower() == "false":
        raise ValueError("教师取码请求未通过；没有取得数字码，停止采样")
    if str(data.get("type")) != "2":
        raise ValueError("当前活动不是数字码签到，停止采样")
    if str(data.get("finish")) != "1":
        return None, {}
    raw_code = data["attendanceCode"]
    if isinstance(raw_code, int) and not isinstance(raw_code, bool) and 0 <= raw_code <= 9999:
        code = f"{raw_code:04d}"
    elif isinstance(raw_code, str) and len(raw_code) == 4 and raw_code.isascii() and raw_code.isdigit():
        code = raw_code
    else:
        raise ValueError("服务端返回的码不是四位数字，停止采样")
    # 不保存响应内的师生名单、身份和凭据，只取码值和刷新元数据。
    return code, {key: data.get(key) for key in ("refreshCode", "refreshCodeTime", "expireTime")}


def collect(session, attendance_id: int, samples: int, output: Path, interval: float = 5) -> dict:
    if samples < 1 or interval < 5:
        raise ValueError("样本数必须为正数，采样间隔不得小于 5 秒")
    data = {"schema": "dgut-digit-api-v1", "source": "teacher-detail-api",
            "startedAt": datetime.now(timezone.utc).isoformat(), "intervalSeconds": interval,
            "observations": []}
    write_samples(output, data)
    next_start = time.monotonic()
    try:
        for index in range(samples):
            if index:
                time.sleep(max(0, next_start - time.monotonic()))
            started = time.monotonic()
            code, metadata = read_code(session, attendance_id)
            if code is None:
                data["stopReason"] = "activity-ended"
                break
            data["observations"].append({"timestampMs": int(time.time() * 1000), "code": code,
                                          "requestDurationMs": round((time.monotonic() - started) * 1000),
                                          **metadata})
            write_samples(output, data)
            print(f"sample {index + 1}/{samples}: {code}", flush=True)
            # 请求超时时跳过原时间槽，不追赶补发。
            next_start = max(started + interval, time.monotonic())
        else:
            data["stopReason"] = "sample-limit"
    except BaseException:
        data["stopReason"] = "interrupted-or-error"
        raise
    finally:
        data["finishedAt"] = datetime.now(timezone.utc).isoformat()
        write_samples(output, data)
    return data


def wait_for_activity(session, classroom_id: int, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while True:
        response = session.get(
            f"https://application.dgut.edu.cn/classroomapi/wisdomClassroom/getClassroomActivitys/{classroom_id}",
            params={"pageSize": 100, "pageNum": 1, "relationType": 1}, timeout=12, allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("教师课堂活动列表读取未通过")
        active = [row for row in data["list"] if str(row.get("relationType")) == "1" and str(row.get("status")) == "0"]
        if len(active) > 1:
            raise ValueError("存在多个进行中的签到活动，请明确指定 --attendance-id")
        if active:
            attendance_id = int(active[0]["relationId"])
            code, metadata = read_code(session, attendance_id)
            if code is not None:
                if str(metadata.get("refreshCode")) != "1" or str(metadata.get("refreshCodeTime")) != "5":
                    raise ValueError("当前数字码未设置为 5 秒刷新，请在教师页设置")
                return attendance_id
        if time.monotonic() >= deadline:
            raise RuntimeError("等待新数字码活动超时，未采集过期活动")
        time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9223)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--attendance-id", type=int)
    target.add_argument("--classroom-id", type=int)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--wait-login", type=int, default=300)
    parser.add_argument("--wait-activity", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (Path(__file__).resolve().parents[2] / ".course-captures" / "code-study" /
                             f"digit-api-{datetime.now():%Y%m%d-%H%M%S}.json")
    login = BrowserSession()
    print(f"Waiting for teacher login on {args.port}...", flush=True)
    try:
        deadline = time.monotonic() + args.wait_login
        while True:
            try:
                identity = login.import_browser(args.port)
                break
            except BrowserSessionError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待教师登录超时；未读取本地登录缓存") from None
                time.sleep(2)
        if identity["roleId"] != 81:
            raise ValueError("该调试浏览器不是已知教师身份，请登录测试课教师账号")
        attendance_id = args.attendance_id
        if args.classroom_id:
            print("Teacher identity verified. Waiting for a live 5-second numeric-code activity...", flush=True)
            attendance_id = wait_for_activity(login._session, args.classroom_id, args.wait_activity)
        print("Teacher identity verified. Sampling server details every 5 seconds.", flush=True)
        data = collect(login._session, attendance_id, args.samples, output)
        if data["observations"]:
            report = analyze(data)
            report_path = output.with_name(output.stem + "-report.json")
            write_samples(report_path, report)
            print(json.dumps({"samples": len(data["observations"]), "report": str(report_path)}, ensure_ascii=False), flush=True)
        else:
            print("Activity ended; no samples collected.", flush=True)
    finally:
        login.clear()


if __name__ == "__main__":
    main()
