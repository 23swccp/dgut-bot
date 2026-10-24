"""主登录与维护脚本共用的学校登录协议常量，不创建或保存会话。"""

LOGIN_URL = "https://lms.dgut.edu.cn/courseapi/users/login/v2"
IDENTITY_URL = "https://application.dgut.edu.cn/classroomapi/users/getUserInfo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
LOGIN_ORIGIN = "https://application.dgut.edu.cn"
LOGIN_REFERER = "https://application.dgut.edu.cn/application"


def login_headers() -> dict[str, str]:
    """账号密码恢复使用表单和学校应用来源，与现有登录接口一致。"""
    return {"Content-Type": "application/x-www-form-urlencoded", "Origin": LOGIN_ORIGIN,
            "Referer": LOGIN_REFERER, "User-Agent": USER_AGENT}
