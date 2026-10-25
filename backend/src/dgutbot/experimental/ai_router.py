"""Bounded failover across the organization's currently enabled AI models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .ulearning_ai import AiModel, UlearningAiError
from .ulearning_ai_bridge import BridgeReply, flatten_messages


class AiReplyValidationError(UlearningAiError):
    """A response can be repaired once before trying another model."""


@dataclass(frozen=True)
class RoutedReply:
    reply: BridgeReply
    model_id: int
    model_name: str
    value: Any = None


class AiRouter:
    def __init__(self, bridge: Any, emit: Callable[[str, str], None]) -> None:
        self.bridge = bridge
        self.emit = emit

    @staticmethod
    def candidates(models: tuple[AiModel, ...], preferred: int) -> list[AiModel]:
        unique = {model.id: model for model in models}
        # 用户首选优先，后备先用本地文字模型，再用其他文字/视觉模型。
        return sorted(unique.values(), key=lambda m: (
            m.id != preferred, m.vision, "本地" not in m.name,
        ))

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: int,
        validate: Callable[[BridgeReply], Any] | None = None,
        repair: Callable[[str], list[dict[str, Any]]] | None = None,
        guard: Callable[[], Any] = lambda: None,
    ) -> RoutedReply:
        # 客户端输入错误不属于模型故障，不能对全部模型重复发送无效输入。
        flatten_messages(messages)
        guard()
        models = self.candidates(self.bridge.models(), int(model_id))
        guard()
        if not models:
            raise UlearningAiError("No enabled AI model is available.")
        failures = []
        for index, model in enumerate(models):
            if index or model.id != model_id:
                self.emit(f"自动切换 AI：{model.name}。", "warn")
            current_messages = messages
            for attempt in range(2 if repair else 1):
                guard()
                self.emit(f"正在请求 {model.name}（尝试 {attempt + 1}/{2 if repair else 1}）。", "info")
                try:
                    reply = self.bridge.complete(current_messages, model_id=model.id)
                    guard()
                    if not reply.text.strip():
                        raise AiReplyValidationError("The AI service returned no answer text.")
                    value = validate(reply) if validate else None
                except AiReplyValidationError as error:
                    guard()
                    self.emit(f"{model.name}：{error}", "warn")
                    if attempt == 0 and repair:
                        current_messages = repair(str(error))
                        continue
                    failures.append(f"{model.name}：{error}")
                    break
                except UlearningAiError as error:
                    # 额度/服务/网络错误不在同一模型上空耗第二次机会。
                    guard()
                    self.emit(f"{model.name}：{error}", "warn")
                    failures.append(f"{model.name}：{error}")
                    break
                else:
                    guard()
                    self.emit(f"AI 响应已通过校验：{model.name}。", "success")
                    return RoutedReply(reply, model.id, model.name, value)
        raise UlearningAiError("所有可用 AI 模型均失败：" + "；".join(failures))
