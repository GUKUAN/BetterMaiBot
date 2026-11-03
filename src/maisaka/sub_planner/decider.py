"""副思考判定器。

它挂在消息入库路径上，必须够快：能靠规则定的当场给结论，需要模型的部分
丢到后台任务里跑。入库路径上还持着 _message_ingest_lock，在这里 await 一次
模型请求会把后面所有消息一起拖住。
"""

from typing import TYPE_CHECKING, Optional

import asyncio
import time

from src.chat.message_receive.message import SessionMessage
from src.common.data_models.llm_service_data_models import LLMGenerationOptions
from src.common.logger import get_logger
from src.config.config import global_config, model_config

from ..monitor.events import emit_sub_planner_decision
from ..planner_task import PlannerTaskState
from .prompt import build_prompt, parse_decision
from .rules import SubDecision, decide_by_rules

if TYPE_CHECKING:
    from ..runtime import MaisakaHeartFlowChatting

logger = get_logger("maisaka_sub_planner")

MODEL_FAILURE_LIMIT = 3
"""连续失败到这个数就不再问模型，剩下的消息按不影响主思考处理。"""


class SubPlannerDecider:
    """跟着一条主思考走，同一条任务共享冷却与判定次数。"""

    def __init__(self, runtime: "MaisakaHeartFlowChatting") -> None:
        self._runtime = runtime
        self._worker: Optional[asyncio.Task[None]] = None
        self._batch: list[SessionMessage] = []
        self._task_id = ""
        self._decided_at = 0.0
        self._decided_in_task = 0
        self._model_failures = 0
        self._client = None
        self._emit_tasks: set[asyncio.Task[None]] = set()

    def observe(self, message: SessionMessage) -> None:
        """登记一条新消息并给出结论；不需要模型的路径当场落地。"""

        task = self._runtime._planner_task
        self._sync_task(task)

        if not global_config.sub_planner.enable:
            # 副思考没开，保持快速反应模式原来的行为
            self._runtime._request_planner_interrupt_for_message(message)
            return

        decision = decide_by_rules(
            message,
            task,
            has_interrupt_budget=self._runtime._planner_interrupt.has_budget_left(),
            cooling_down=self._is_cooling_down(),
        )
        if decision is not None:
            self._apply(task, [message], decision)
            return

        if not self._model_allowed(task):
            # 规则拿不准、模型又问不了：按不影响主思考处理，消息留在缓存里等主循环接走
            return

        self._batch.append(message)
        self._ensure_worker()

    def _sync_task(self, task: Optional[PlannerTaskState]) -> None:
        """换了主思考就把冷却和判定次数清掉。"""

        current = "" if task is None else task.task_id
        if current == self._task_id:
            return
        self._task_id = current
        self._decided_at = 0.0
        self._decided_in_task = 0
        self._model_failures = 0
        self._batch.clear()

    def _is_cooling_down(self) -> bool:
        cooldown = global_config.sub_planner.cooldown_seconds
        if cooldown <= 0:
            return False
        return (time.time() - self._decided_at) < cooldown

    def _model_allowed(self, task: Optional[PlannerTaskState]) -> bool:
        if task is None:
            return False
        if global_config.sub_planner.rule_only:
            return False
        if self._decided_in_task >= global_config.sub_planner.max_decisions_per_task:
            return False
        return self._model_failures < MODEL_FAILURE_LIMIT

    def _apply(
        self,
        task: Optional[PlannerTaskState],
        messages: list[SessionMessage],
        decision: SubDecision,
        duration_ms: Optional[float] = None,
    ) -> None:
        self._emit_decision(
            method="model" if not decision.by_rule else "rule",
            kind=decision.kind,
            reason=decision.reason,
            messages=messages,
            duration_ms=duration_ms,
        )
        if decision.kind == "interrupt":
            self._runtime._request_planner_interrupt_for_message(messages[-1])
            return
        if task is None:
            return
        if decision.kind == "update":
            task.note_update(self._describe(messages, decision.reason))
        elif decision.kind == "defer":
            for message in messages:
                task.note_deferred(message.message_id)
        # continue：什么都不做，消息留在缓存里等主循环到安全节点自己接走

    def _emit_decision(
        self,
        *,
        method: str,
        kind: str,
        reason: str,
        messages: list[SessionMessage],
        duration_ms: Optional[float] = None,
    ) -> None:
        """把判定广播给 WebUI；发不出去绝不影响主流程。"""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        model_name = self._model_name() if method == "model" else ""
        coro = emit_sub_planner_decision(
            session_id=str(self._runtime.session_id or ""),
            run_id=self._runtime._monitor_run_id,
            cycle_id=getattr(self._runtime, "_cycle_counter", None),
            method=method,
            kind=kind,
            reason=reason,
            trigger_summary=self._describe(messages, "")[:200],
            messages_count=len(messages),
            model_name=model_name,
            duration_ms=duration_ms,
        )
        task = loop.create_task(coro)
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    def _model_name(self) -> str:
        """当前副思考用的模型名，取不到就空着。"""

        try:
            names = model_config.model_task_config.sub_planner.model_list
            return str(names[0]) if names else ""
        except Exception:
            return ""

    @staticmethod
    def _describe(messages: list[SessionMessage], reason: str) -> str:
        parts = []
        for message in messages:
            who = message.message_info.user_info.user_nickname or "某人"
            text = (message.processed_plain_text or "").strip().replace("\n", " ")
            if len(text) > 80:
                text = text[:80] + "…"
            parts.append(f"{who}：{text}")
        return f"{'；'.join(parts)}（{reason}）"

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._run_decisions())

    async def _run_decisions(self) -> None:
        """后台逐个判定批次；模型慢或失败都不影响主循环。"""

        try:
            while self._batch and self._runtime._running:
                limit = max(1, global_config.sub_planner.max_batch_messages)
                batch = self._batch[:limit]
                del self._batch[: len(batch)]

                task = self._runtime._planner_task
                if task is None or task.task_id != self._task_id:
                    return

                snapshot = (task.task_id, task.version)
                started = time.time()
                try:
                    decision = await asyncio.wait_for(
                        self._decide_by_model(batch, task),
                        timeout=global_config.sub_planner.timeout_seconds,
                    )
                except TimeoutError:
                    logger.info(f"{self._runtime.log_prefix} 副思考判定超时，按不影响主思考处理")
                    self._emit_decision(
                        method="model", kind="timeout", reason="判定超时",
                        messages=batch,
                        duration_ms=(time.time() - started) * 1000,
                        model_name=self._model_name(),
                    )
                    decision = None
                except Exception as exc:
                    self._model_failures += 1
                    logger.warning(f"{self._runtime.log_prefix} 副思考判定失败: {exc}")
                    self._emit_decision(
                        method="model", kind="failed", reason=f"判定失败: {exc}",
                        messages=batch,
                        duration_ms=(time.time() - started) * 1000,
                        model_name=self._model_name(),
                    )
                    continue

                self._decided_at = time.time()
                self._decided_in_task += 1

                if decision is None:
                    continue
                if self._is_stale(snapshot):
                    logger.debug(f"{self._runtime.log_prefix} 副思考结论已过期，丢弃")
                    continue
                self._apply(
                    task, batch, decision,
                    duration_ms=(time.time() - started) * 1000,
                )
        finally:
            self._worker = None

    def _is_stale(self, snapshot: tuple[str, int]) -> bool:
        """判定期间主任务已经换了或者是新版本，这条结论就不能用了。"""

        task = self._runtime._planner_task
        if task is None:
            return True
        task_id, version = snapshot
        return task.task_id != task_id or task.version != version

    async def _decide_by_model(
        self,
        batch: list[SessionMessage],
        task: PlannerTaskState,
    ) -> Optional[SubDecision]:
        """问一次轻量模型；拿不到结论就当作不影响主思考。"""

        client = self._get_client()
        if client is None:
            return None

        prompt = build_prompt(batch, task)
        try:
            # 判定只输出一小段 JSON，输出上限压到 256；温度交给任务配置，别在这里写死
            result = await client.generate_response(
                prompt,
                options=LLMGenerationOptions(max_tokens=256),
                session_id=str(self._runtime.session_id or ""),
            )
        except Exception as exc:
            self._model_failures += 1
            logger.warning(f"{self._runtime.log_prefix} 副思考模型请求失败: {exc}")
            return None

        decision = parse_decision(result.response)
        if decision is None:
            logger.info(f"{self._runtime.log_prefix} 副思考判定结果无法解析，按不影响主思考处理")
            return None

        logger.info(
            f"{self._runtime.log_prefix} 副思考判定={decision.kind} 原因={decision.reason} 消息={len(batch)} 条"
        )
        return decision

    def _get_client(self):
        """按需创建判定客户端；没配模型时返回 None，退回规则层。"""

        if self._client is not None:
            return self._client
        if not model_config.model_task_config.sub_planner.model_list:
            return None

        # 放在函数里导入：llm_service 会拉起整条编排链，模块级导入容易和 runtime 撞上
        from src.services.llm_service import LLMServiceClient

        try:
            self._client = LLMServiceClient(
                task_name="sub_planner",
                request_type="maisaka.sub_planner",
                session_id=str(self._runtime.session_id or ""),
            )
        except Exception as exc:
            logger.warning(f"{self._runtime.log_prefix} 副思考模型客户端创建失败: {exc}")
            return None
        return self._client
