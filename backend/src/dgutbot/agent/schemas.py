"""Public DTO schemas. No browser target or credential fields are permitted."""

S = {"type": "string"}
B = {"type": "boolean"}
I = {"type": "integer"}
N = {"type": "number"}
NULL = {"type": "null"}


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


def nullable(schema):
    return {"anyOf": [schema, NULL]}


ERROR = obj({"code": S, "message": S, "retryable": B, "details": {"type": "object"},
             "recovery": obj({"tool": S, "input": {"type": "object"}})}, ["code", "message", "retryable", "details"])
PAGE = obj({"id": S, "name": S, "index": I, "total": I}, [])
COURSE = obj({"id": S, "name": S, "teacherName": S})
TASK = obj({
    "taskId": S, "kind": {"enum": ["course"]},
    "state": {"enum": ["queued", "running", "waiting_for_input", "completed", "failed", "cancelled"]},
    "revision": {"type": "integer", "minimum": 1}, "createdAt": S, "updatedAt": S,
    "progress": obj({"current": I, "total": I, "unit": {"const": "page"}}),
    "waiting": nullable(obj({"type": {"const": "quiz_answers"}, "requestId": S})),
    "result": nullable(obj({"completed": B})), "error": nullable(ERROR),
})
QUIZ_STATE = {"enum": ["pending", "validating", "staged", "applying", "submitting", "completed", "rejected", "expired", "failed", "cancelled"]}
QUESTION = obj({"id": S, "type": {"enum": ["single_choice", "multiple_choice", "true_false", "fill_blank", "unsupported"]},
                "sourceType": S, "prompt": S, "options": array(obj({"id": S, "text": S})), "blankCount": I,
                "hasMedia": B, "answerSchema": {"type": "object"}})
QUIZ = obj({"requestId": S, "revision": I, "taskId": S, "sessionId": S, "pageId": S, "state": QUIZ_STATE,
            "createdAt": S, "expiresAt": S, "submitPolicy": {"const": "apply_and_commit"}, "questions": array(QUESTION)})
QUIZ_RESULT = obj({"requestId": S, "revision": I, "state": QUIZ_STATE,
                   "result": nullable(obj({"completedCount": I, "submitAttempts": {"const": 1}})), "error": nullable(ERROR)})
EVENT_DATA = obj({"requestId": S, "questionCount": I, "count": I, "playbackRate": N,
                  "completed": I, "skipped": I, "failed": I, "elapsedSeconds": N}, [])
EVENT = obj({"seq": I, "time": S, "sessionId": S, "code": S, "level": S, "category": S, "message": S, "page": PAGE, "data": EVENT_DATA})
TOOL = obj({"name": S, "description": S, "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"},
            "readOnly": B, "idempotent": B, "taskBehavior": {"enum": ["none", "creates_task", "waits_bounded", "cancels_task"]}})
OUTPUTS = {
    "system.version": obj({"appName": S, "appVersion": S, "schemaVersion": {"type": "integer", "const": 1}, "instanceId": S}),
    "system.health": obj({"service": {"const": "ready"}, "loggedIn": B, "coursesLoaded": B, "courseSelected": B,
                          "taskManager": obj({"ready": B, "active": B}),
                          "courseController": obj({"running": B, "connected": B, "state": S})}),
    "system.capabilities": obj({"schemaVersion": {"type": "integer", "const": 1}, "tools": array(TOOL)}),
    "course.get_status": obj({
        "running": B, "completed": B, "connected": B, "state": S, "sessionId": S, "courseName": S,
        "page": obj({"id": S, "name": S, "index": I, "total": I, "completed": B}),
        "currentTask": S, "pagePlan": array(obj({"kind": S, "state": S, "count": I})),
        "video": obj({"currentTime": N, "duration": N, "rate": N}),
        "stalled": B, "stallReason": S, "retryCount": I, "taskId": nullable(S),
    }),
    "session.load_courses": obj({"courses": array(COURSE)}), "course.list": obj({"courses": array(COURSE)}),
    "course.select": obj({"course": COURSE}), "course.start": TASK, "course.stop": obj({"stopped": B, "taskId": nullable(S)}),
    "course.set_speed": obj({"rate": N}), "task.get": TASK, "task.cancel": TASK,
    "task.wait": obj({"task": TASK, "timedOut": B}),
    "events.wait": obj({"events": array(EVENT), "nextSeq": I, "timedOut": B, "droppedBeforeSeq": I}),
    "quiz.list_pending": obj({"requests": array(obj({"requestId": S, "revision": I, "taskId": S, "sessionId": S,
                              "pageId": S, "state": QUIZ_STATE, "expiresAt": S, "questionCount": I}))}),
    "quiz.get_request": QUIZ, "quiz.get_result": QUIZ_RESULT,
    "quiz.validate_answers": obj({"requestId": S, "revision": I, "valid": B, "state": QUIZ_STATE}),
    "quiz.submit_answers": obj({"requestId": S, "revision": I, "state": QUIZ_STATE, "valid": B}, ["requestId", "revision", "state"]),
}
