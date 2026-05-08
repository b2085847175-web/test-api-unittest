import os
import random
import re
import time
import unittest
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import yaml

from api_object.auth_api import AuthAPI
from api_object.chat_api import ChatAPI
from api_object.quality_inspection_api import QualityInspectionAPI
from common.http_client import create_http_client
from config.context_runtime import load_context_runtime
from config.project_env import resolve_effective_env
from testcases.unittest_helpers import bind_case_tests

# Stable defaults for this module: YAML only needs `run_times + cases`.
# If needed, these can be adjusted in code without increasing YAML complexity.
DEFAULT_RUN_TIMES = 5
RUN_INTERVAL_SECONDS = 1.0
TURN_INTERVAL_SECONDS = 1.0
MIN_PASS_RATE = 0.8
CONTINUE_ON_FAILURE = True


def _to_int(value: Any, default: int, *, min_value: Optional[int] = None) -> int:
    if value is None:
        parsed = default
    else:
        parsed = int(value)
    if min_value is not None and parsed < min_value:
        raise ValueError(f"expected int >= {min_value}, got {parsed}")
    return parsed


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _load_stability_suite() -> Dict[str, Any]:
    """Load minimal stability config: `run_times` and `cases`."""
    # 业务配置入口：稳定性套件只关心运行次数和待重复验证的 case。
    cases_file = os.getenv("CHAT_HIT_STABILITY_CASES_FILE", "chat_hit_stability_cases.yaml")
    if os.path.isabs(cases_file):
        data_path = cases_file
    else:
        data_path = os.path.join(os.path.dirname(__file__), "..", "data", cases_file)

    with open(data_path, "r", encoding="utf-8") as file:
        suite = yaml.safe_load(file) or {}

    target_env = resolve_effective_env(str(suite.get("target_env", "")).strip().lower() or os.getenv("ENV", "dev"))
    if target_env not in {"dev", "console"}:
        raise ValueError(f"stability target_env must be dev or console, got: {suite.get('target_env')}")

    cases = suite.get("cases") or []
    if not isinstance(cases, list):
        raise ValueError("stability suite cases must be a list")

    run_times = _to_int(suite.get("run_times"), DEFAULT_RUN_TIMES, min_value=1)

    return {
        "target_env": target_env,
        "cases_file": data_path,
        "run_times": run_times,
        "cases": cases,
    }


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("gbk", errors="backslashreplace").decode("gbk"))


def build_runtime_username(case_name: str, run_index: int) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", case_name.lower()).strip("_")
    short_name = normalized[:10] or "case"
    timestamp_ms = int(time.time() * 1000)
    rand4 = f"{random.getrandbits(16):04x}"
    return f"stb_{short_name}_r{run_index}_{timestamp_ms}_{rand4}"[:40]


def _normalize_expect(expect_data: Any) -> Dict[str, Any]:
    """归一化稳定性 case 的期望断言结构。"""
    if isinstance(expect_data, str):
        return {
            "knowledge": {
                "stats_contains": {
                    "scene_knowledge": [expect_data],
                }
            }
        }

    if not isinstance(expect_data, dict):
        return {}

    normalized = dict(expect_data)
    scene_alias = normalized.get("scene")
    if scene_alias is not None:
        scene_values = scene_alias if isinstance(scene_alias, list) else [scene_alias]
        knowledge = normalized.setdefault("knowledge", {})
        stats_contains = knowledge.setdefault("stats_contains", {})
        stats_contains["scene_knowledge"] = [str(item) for item in scene_values]
    return normalized


def _assert_contains_subset(case_label: str, field_name: str, expected_values: List[str], actual_values: List[str]) -> None:
    actual_list = list(actual_values or [])
    for expected in expected_values or []:
        assert expected in actual_list, (
            f"[{case_label}] {field_name} missing expected value: {expected} | actual: {actual_list}"
        )


def _scene_expected_hit(expected_scene: str, actual_scenes: List[str]) -> bool:
    expected_scene = str(expected_scene or "").strip()
    if not expected_scene:
        return True
    if not actual_scenes:
        return False
    normalized_actuals = [str(scene or "").strip() for scene in actual_scenes if str(scene or "").strip()]
    return expected_scene in normalized_actuals


def _assert_reply(case_label: str, expected: Dict[str, Any], chat_reply: str, final_reply: str) -> None:
    for expected_text in expected.get("reply_contains", []):
        assert expected_text in chat_reply, (
            f"[{case_label}] chat reply missing expected text: {expected_text} | actual: {chat_reply}"
        )
        assert expected_text in final_reply, (
            f"[{case_label}] quality final_reply missing expected text: {expected_text} | actual: {final_reply}"
        )


def _assert_quality(case_label: str, expected: Dict[str, Any], normalized_record: Dict[str, Any]) -> None:
    quality_expect = expected.get("quality", {})
    expected_level = quality_expect.get("level")
    if expected_level:
        assert normalized_record["level"] == expected_level, (
            f"[{case_label}] level mismatch | expected: {expected_level} | actual: {normalized_record['level']}"
        )
    _assert_contains_subset(
        case_label,
        "categories",
        quality_expect.get("categories_contains", []),
        normalized_record["categories"],
    )


def _assert_stats(case_label: str, expected: Dict[str, Any], normalized_record: Dict[str, Any]) -> None:
    stats_expect = expected.get("knowledge", {}).get("stats_contains", {})
    stats_map = normalized_record["stats_map"]
    for stat_key, expected_names in stats_expect.items():
        expected_list = expected_names if isinstance(expected_names, list) else [expected_names]
        actual_values = stats_map.get(stat_key, [])
        if stat_key == "scene_knowledge":
            for expected_scene in expected_list:
                assert _scene_expected_hit(str(expected_scene), actual_values), (
                    f"[{case_label}] stats.scene_knowledge missing expected scene: {expected_scene} | "
                    f"actual: {actual_values}"
                )
            continue
        _assert_contains_subset(case_label, f"stats.{stat_key}", expected_list, actual_values)


def _assert_details(case_label: str, expected: Dict[str, Any], normalized_record: Dict[str, Any]) -> None:
    details_expect = expected.get("knowledge", {}).get("details_contains", {})
    details_map = normalized_record["details_map"]
    for detail_key, expected_values in details_expect.items():
        actual_values = details_map.get(detail_key, [])
        for expected_value in expected_values:
            assert any(expected_value in actual for actual in actual_values), (
                f"[{case_label}] details.{detail_key} missing expected text: {expected_value} | "
                f"actual: {actual_values}"
            )


def _assert_actions(case_label: str, expected: Dict[str, Any], normalized_record: Dict[str, Any]) -> None:
    actions_expect = expected.get("actions", {})
    _assert_contains_subset(case_label, "actions.types", actions_expect.get("types", []), normalized_record["action_types"])
    _assert_contains_subset(
        case_label,
        "actions.forward_scenes",
        actions_expect.get("forward_scenes", []),
        normalized_record["forward_scenes"],
    )


def _normalize_failure_reason(error_text: str) -> str:
    """归一化失败原因，便于稳定性汇总时统计 Top 失败类型。"""
    reason = str(error_text or "").strip()
    if not reason:
        return "unknown_failure"
    if "] " in reason:
        reason = reason.split("] ", 1)[1]
    if " | " in reason:
        reason = reason.split(" | ", 1)[0]
    return reason[:160]


def _query_turn_quality(
    quality_client: QualityInspectionAPI,
    runtime_username: str,
    shop_id: str,
    turn_send_at: float,
    turn_response_at: float,
    chat_reply: str,
    chat_response: Dict[str, Any],
    user_message: str,
    case_name: str,
    run_index: int,
    turn_index: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """查询并匹配稳定性场景中单轮对话对应的质检记录。"""
    selected_record = None
    quality_response = None
    for attempt in range(1, 11):
        query_start = int(turn_send_at) - 5
        query_end = int(turn_response_at) + 30 + (attempt * 2)
        _safe_print(
            f"TURN_QUALITY_WINDOW case_name={case_name} run={run_index} runtime_username={runtime_username} "
            f"turn_index={turn_index} startTime={query_start} endTime={query_end}"
        )
        quality_response = quality_client.get_user_detail(
            username=runtime_username,
            shop_id=shop_id,
            start_time=query_start,
            end_time=query_end,
        )
        _safe_print(
            f"TURN_QUALITY_QUERY case_name={case_name} run={run_index} runtime_username={runtime_username} "
            f"turn_index={turn_index} attempt={attempt} url=/api/quality-inspection/user-detail "
            f"params={quality_response['params']}"
        )
        _safe_print(
            f"TURN_QUALITY_RESPONSE case_name={case_name} run={run_index} runtime_username={runtime_username} "
            f"turn_index={turn_index} status={quality_response['status_code']} "
            f"code={quality_response['code']} msg={quality_response['msg']} "
            f"records={len(quality_response['records'])}"
        )

        assert quality_response["status_code"] == 200
        assert quality_response["code"] == 200

        selected_record = quality_client.find_best_matching_record(
            quality_response["records"],
            runtime_username,
            shop_id,
            turn_send_at,
            turn_response_at,
            chat_reply,
            chat_response=chat_response,
            user_message=user_message,
        )
        if selected_record:
            break
        time.sleep(2)

    return quality_response, selected_record


def _normalize_context_messages(case_name: str, raw_messages: Any) -> List[Dict[str, str]]:
    """归一化稳定性 case 可选的历史上下文消息。"""
    raw_list = raw_messages or []
    if not isinstance(raw_list, list):
        raise ValueError(f"[{case_name}] context_messages must be a list")

    context_messages: List[Dict[str, str]] = []
    for index, message in enumerate(raw_list, start=1):
        if not isinstance(message, dict):
            raise ValueError(f"[{case_name}] context_messages[{index}] must be a dict")
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", "")).strip()
        if role not in {"user", "assistant"}:
            raise ValueError(f"[{case_name}] context_messages[{index}].role must be user or assistant")
        if not content:
            raise ValueError(f"[{case_name}] context_messages[{index}].content cannot be empty")
        context_messages.append({"role": role, "content": content})
    return context_messages


def _normalize_turns(case_name: str, case_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """归一化稳定性 case 的对话轮次，兼容 turns/request/questions 三种写法。"""
    # Keep compatibility with existing data styles in the repo.
    # Preferred structure is `turns`, but `request`/`questions` are also accepted.
    if "turns" in case_data:
        raw_turns = case_data.get("turns") or []
        if not isinstance(raw_turns, list):
            raise ValueError(f"[{case_name}] turns must be a list")

        turns: List[Dict[str, Any]] = []
        for index, turn in enumerate(raw_turns, start=1):
            if isinstance(turn, str):
                turns.append({"question": turn, "expect": {}})
                continue
            if not isinstance(turn, dict):
                raise ValueError(f"[{case_name}] turns[{index}] must be a string or dict")
            question = str(turn.get("question") or turn.get("message") or "").strip()
            if not question:
                raise ValueError(f"[{case_name}] turns[{index}].question is required")
            turns.append({"question": question, "expect": _normalize_expect(turn.get("expect", {}))})

        if not turns:
            raise ValueError(f"[{case_name}] turns cannot be empty")
        return turns

    if "request" in case_data:
        request_data = case_data.get("request") or {}
        if not isinstance(request_data, dict):
            raise ValueError(f"[{case_name}] request must be a dict")
        questions = request_data.get("questions")
        if not questions and request_data.get("message"):
            questions = [request_data["message"]]
        if not isinstance(questions, list) or not questions:
            raise ValueError(f"[{case_name}] request.questions or request.message is required")

        turns = [{"question": str(question).strip(), "expect": {}} for question in questions if str(question).strip()]
        if not turns:
            raise ValueError(f"[{case_name}] request questions cannot be empty")

        expect_data = _normalize_expect(case_data.get("expect", {}))
        if expect_data:
            turns[-1]["expect"] = expect_data
        return turns

    questions = case_data.get("questions") or []
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"[{case_name}] supports turns/request/questions; at least one must be present")

    turns = [{"question": str(question).strip(), "expect": {}} for question in questions if str(question).strip()]
    if not turns:
        raise ValueError(f"[{case_name}] questions cannot be empty")
    expect_data = _normalize_expect(case_data.get("expect", {}))
    if expect_data:
        turns[-1]["expect"] = expect_data
    return turns


def _normalize_stability_case(case_data: Dict[str, Any]) -> Dict[str, Any]:
    """归一化单条稳定性业务 case。"""
    if not isinstance(case_data, dict):
        raise ValueError(f"stability case must be a dict, got: {type(case_data).__name__}")

    case_name = str(case_data.get("name", "")).strip()
    if not case_name:
        raise ValueError("stability case name is required")

    turns = _normalize_turns(case_name, case_data)
    context_messages = _normalize_context_messages(case_name, case_data.get("context_messages"))

    return {
        "name": case_name,
        "context_messages": context_messages,
        "turns": turns,
    }


def _prepare_context_messages(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """为稳定性场景的历史上下文消息补充 created_at。"""
    if not messages:
        return []
    start_at = time.time() - len(messages) - 5
    prepared: List[Dict[str, Any]] = []
    for index, message in enumerate(messages):
        prepared.append(
            {
                "role": message["role"],
                "content": message["content"],
                "created_at": start_at + index,
            }
        )
    return prepared


def _build_auth_header(access_token: str) -> str:
    normalized = str(access_token or "").strip()
    if not normalized:
        return normalized
    if normalized.lower().startswith("bearer "):
        return normalized
    return f"Bearer {normalized}"


_STABILITY_SUITE = _load_stability_suite()
_STABILITY_CASE_IDS = [
    case.get("name", f"stability_case_{index}") if isinstance(case, dict) else f"stability_case_{index}"
    for index, case in enumerate(_STABILITY_SUITE["cases"], start=1)
]
_STABILITY_CASES = _STABILITY_SUITE["cases"]
_SUITE_CASE_SUMMARIES: List[Dict[str, Any]] = []


def _emit_stability_suite_summary() -> None:
    """输出整套稳定性业务场景的汇总命中率。"""
    if not _SUITE_CASE_SUMMARIES:
        return

    total_cases = len(_SUITE_CASE_SUMMARIES)
    total_runs = sum(item["total_runs"] for item in _SUITE_CASE_SUMMARIES)
    hit_runs = sum(item["hit_runs"] for item in _SUITE_CASE_SUMMARIES)
    weighted_hit_probability = (hit_runs / total_runs) if total_runs else 0.0

    unstable_cases = [
        f"{item['name']}({_pct(item['hit_probability'])}<{_pct(item['min_pass_rate'])})"
        for item in _SUITE_CASE_SUMMARIES
        if item["hit_probability"] < item["min_pass_rate"]
    ]

    _safe_print(
        f"SUITE_STABILITY_SUMMARY total_cases={total_cases} total_runs={total_runs} "
        f"hit_runs={hit_runs} weighted_hit_probability={_pct(weighted_hit_probability)}"
    )
    if unstable_cases:
        _safe_print(f"SUITE_STABILITY_SUMMARY unstable_cases={unstable_cases}")
    else:
        _safe_print("SUITE_STABILITY_SUMMARY unstable_cases=[]")


def _create_stability_access_token(stability_runtime: Dict[str, Any]) -> str:
    """认证动作：根据稳定性流程运行环境获取访问令牌。"""
    if stability_runtime["auth_mode"] == "token":
        return stability_runtime["access_token"]

    auth_client_http = create_http_client(
        base_url=stability_runtime["api_base_url"],
        default_headers=stability_runtime["headers"],
    )
    auth_client = AuthAPI(client=auth_client_http)
    try:
        _safe_print(
            f"STABILITY_LOGIN_REQUEST env={stability_runtime['target_env']} "
            f"account={stability_runtime['login_account']}"
        )
        login_response = auth_client.login(
            stability_runtime["login_account"],
            stability_runtime["login_password"],
        )
        _safe_print(
            f"STABILITY_LOGIN_RESPONSE env={stability_runtime['target_env']} "
            f"status={login_response['status_code']} body={login_response['data']}"
        )
        assert login_response["status_code"] == 200
        assert login_response["data"].get("code") == 200
        access_token = login_response.get("access_token")
        assert access_token, "stability login succeeded but accessToken was empty"
        return access_token
    finally:
        auth_client_http.close()


class ChatHitStabilityScenario:
    """业务场景执行器：多次重复运行同一 case，统计命中稳定性。

    该对象负责稳定性业务逻辑，包括多轮请求、逐轮断言、命中率统计和失败原因归因。
    unittest 测试类只负责环境、登录和 client 生命周期。
    """

    def __init__(self, authenticated_apis: Dict[str, Any]) -> None:
        """保存稳定性业务场景需要使用的 API 对象和运行态配置。"""
        self.authenticated_apis = authenticated_apis

    def run_case(self, case_data: Dict[str, Any]) -> None:
        """执行单条稳定性 YAML case。"""
        _run_chat_hit_stability_flow(self.authenticated_apis, case_data)


def _run_chat_hit_stability_flow(stability_authenticated_apis: Dict[str, Any], case_data: Dict[str, Any]) -> None:
    """稳定性业务流程：重复运行同一 case，统计命中率并校验阈值。"""
    chat_client: ChatAPI = stability_authenticated_apis["chat_api"]
    quality_client: QualityInspectionAPI = stability_authenticated_apis["quality_inspection_api"]
    runtime = stability_authenticated_apis["runtime"]

    normalized_case = _normalize_stability_case(case_data)
    case_name = normalized_case["name"]
    shop_id = runtime["shop_id"]
    turns = normalized_case["turns"]
    run_times = _STABILITY_SUITE["run_times"]
    run_interval_seconds = RUN_INTERVAL_SECONDS
    turn_interval_seconds = TURN_INTERVAL_SECONDS
    min_pass_rate = MIN_PASS_RATE
    continue_on_failure = CONTINUE_ON_FAILURE

    account = runtime["chat_account"]
    platform = runtime["platform"]
    is_test = runtime["is_test"]
    shop_name = runtime["shop_name"]

    _safe_print(
        f"CASE_STABILITY_CONFIG case_name={case_name} shop_id={shop_id} turns_count={len(turns)} "
        f"run_times={run_times} min_pass_rate={_pct(min_pass_rate)} "
        f"run_interval_seconds={run_interval_seconds} turn_interval_seconds={turn_interval_seconds} "
        f"continue_on_failure={continue_on_failure} target_env={runtime['target_env']}"
    )

    hit_runs = 0
    turn_stats: Dict[int, Dict[str, int]] = {index: {"hit": 0, "total": 0} for index in range(1, len(turns) + 1)}
    failure_counter: Counter[str] = Counter()

    for run_index in range(1, run_times + 1):
        run_started_at = time.time()
        runtime_username = build_runtime_username(case_name, run_index)
        conversation_messages = _prepare_context_messages(normalized_case["context_messages"])
        run_turn_failures: List[str] = []
        turn_pass_count = 0

        _safe_print(
            f"RUN_START case_name={case_name} run={run_index}/{run_times} runtime_username={runtime_username} "
            f"context_count={len(normalized_case['context_messages'])}"
        )

        for turn_index, turn in enumerate(turns, start=1):
            question = turn["question"]
            turn_expect = _normalize_expect(turn.get("expect", {}))
            turn_stats[turn_index]["total"] += 1
            turn_send_at = time.time()
            user_created_at = turn_send_at

            try:
                conversation_messages.append(
                    {
                        "role": "user",
                        "content": question,
                        "created_at": user_created_at,
                    }
                )
                _safe_print(
                    f"TURN_CONTEXT case_name={case_name} run={run_index} runtime_username={runtime_username} "
                    f"turn_index={turn_index} question={question} request_messages_count={len(conversation_messages)}"
                )

                chat_response = chat_client.chat_answer(
                    account=account,
                    messages=list(conversation_messages),
                    platform=platform,
                    shop_id=shop_id,
                    shop_name=shop_name,
                    username=runtime_username,
                    is_test=is_test,
                    last_order_time=user_created_at,
                )
                turn_response_at = time.time()

                _safe_print(f"CHAT_REQUEST payload={chat_response['payload']}")
                _safe_print(f"CHAT_RESPONSE status={chat_response['status_code']} body={chat_response['data']}")

                assert chat_response["status_code"] == 200
                assert chat_response["data"].get("code") == 200

                assistant_messages = chat_client.extract_assistant_messages(
                    chat_response,
                    response_received_at=turn_response_at,
                )
                assert assistant_messages, f"[{case_name}] run {run_index} turn {turn_index} returned no assistant messages"
                conversation_messages.extend(assistant_messages)

                chat_reply = chat_client.extract_ai_reply(chat_response) or assistant_messages[-1]["content"]
                quality_response, selected_record = _query_turn_quality(
                    quality_client=quality_client,
                    runtime_username=runtime_username,
                    shop_id=shop_id,
                    turn_send_at=turn_send_at,
                    turn_response_at=turn_response_at,
                    chat_reply=chat_reply,
                    chat_response=chat_response,
                    user_message=question,
                    case_name=case_name,
                    run_index=run_index,
                    turn_index=turn_index,
                )

                assert quality_response is not None
                assert selected_record is not None, (
                    f"[{case_name}] run {run_index} turn {turn_index} did not match quality record"
                )

                normalized_turn_record = quality_client.normalize_quality_record(selected_record)
                final_response_text = normalized_turn_record["final_reply"]
                assert final_response_text, (
                    f"[{case_name}] run {run_index} turn {turn_index} quality final_response was empty"
                )
                assert final_response_text == chat_reply, (
                    f"[{case_name}] run {run_index} turn {turn_index} chat reply and quality final_reply mismatch | "
                    f"chat: {chat_reply} | quality: {final_response_text}"
                )

                if turn_expect:
                    case_label = f"{case_name}|run={run_index}|turn={turn_index}"
                    _assert_reply(case_label, turn_expect, chat_reply, final_response_text)
                    _assert_quality(case_label, turn_expect, normalized_turn_record)
                    _assert_stats(case_label, turn_expect, normalized_turn_record)
                    _assert_details(case_label, turn_expect, normalized_turn_record)
                    _assert_actions(case_label, turn_expect, normalized_turn_record)

                turn_pass_count += 1
                turn_stats[turn_index]["hit"] += 1
                _safe_print(
                    f"TURN_ASSERT_RESULT case_name={case_name} run={run_index} turn_index={turn_index} result=PASS"
                )
            except AssertionError as exc:
                reason = _normalize_failure_reason(str(exc))
                failure_counter[reason] += 1
                run_turn_failures.append(f"turn={turn_index} question={question} reason={reason}")
                _safe_print(
                    f"TURN_ASSERT_RESULT case_name={case_name} run={run_index} turn_index={turn_index} "
                    f"result=FAIL detail={reason}"
                )
                if not continue_on_failure:
                    _safe_print(
                        f"RUN_EARLY_STOP case_name={case_name} run={run_index} "
                        f"trigger_turn={turn_index} reason={reason}"
                    )
                    break
            except Exception as exc:  # pragma: no cover
                reason = _normalize_failure_reason(f"{type(exc).__name__}: {exc}")
                failure_counter[reason] += 1
                run_turn_failures.append(f"turn={turn_index} question={question} reason={reason}")
                _safe_print(
                    f"TURN_ASSERT_RESULT case_name={case_name} run={run_index} turn_index={turn_index} "
                    f"result=ERROR detail={reason}"
                )
                if not continue_on_failure:
                    _safe_print(
                        f"RUN_EARLY_STOP case_name={case_name} run={run_index} "
                        f"trigger_turn={turn_index} reason={reason}"
                    )
                    break

            if turn_index < len(turns) and turn_interval_seconds > 0:
                _safe_print(
                    f"TURN_WAIT case_name={case_name} run={run_index} turn_index={turn_index} "
                    f"sleep_seconds={turn_interval_seconds}"
                )
                time.sleep(turn_interval_seconds)

        run_hit = not run_turn_failures
        if run_hit:
            hit_runs += 1

        run_elapsed_ms = int((time.time() - run_started_at) * 1000)
        cumulative_probability = hit_runs / run_index
        first_reason = run_turn_failures[0] if run_turn_failures else "-"

        _safe_print(
            # RUN_RESULT is per-run visibility: whether this run hit, plus cumulative hit probability.
            f"RUN_RESULT case_name={case_name} run={run_index}/{run_times} runtime_username={runtime_username} "
            f"hit={int(run_hit)} turn_pass={turn_pass_count}/{len(turns)} "
            f"duration_ms={run_elapsed_ms} cumulative_hit_probability={_pct(cumulative_probability)} "
            f"reason={first_reason}"
        )

        if run_index < run_times and run_interval_seconds > 0:
            _safe_print(
                f"RUN_WAIT case_name={case_name} run={run_index}/{run_times} sleep_seconds={run_interval_seconds}"
            )
            time.sleep(run_interval_seconds)

    hit_probability = hit_runs / run_times
    turn_probability_map = {
        f"turn_{turn_index}": _pct(item["hit"] / item["total"]) if item["total"] else "0.00%"
        for turn_index, item in turn_stats.items()
    }
    top_fail_reasons = [f"{reason}:{count}" for reason, count in failure_counter.most_common(5)]

    _safe_print(
        # Core stability metric for this case.
        f"CASE_STABILITY_SUMMARY case_name={case_name} total_runs={run_times} "
        f"hit_runs={hit_runs} hit_probability={_pct(hit_probability)} "
        f"threshold={_pct(min_pass_rate)} result={'PASS' if hit_probability >= min_pass_rate else 'FAIL'}"
    )
    _safe_print(
        f"CASE_STABILITY_SUMMARY case_name={case_name} turn_hit_probability={turn_probability_map}"
    )
    _safe_print(
        f"CASE_STABILITY_SUMMARY case_name={case_name} "
        f"top_fail_reasons={top_fail_reasons if top_fail_reasons else []}"
    )

    _SUITE_CASE_SUMMARIES.append(
        {
            "name": case_name,
            "total_runs": run_times,
            "hit_runs": hit_runs,
            "hit_probability": hit_probability,
            "min_pass_rate": min_pass_rate,
        }
    )

    assert hit_probability >= min_pass_rate, (
        f"[{case_name}] stability below threshold | hit_probability={_pct(hit_probability)} "
        f"< threshold={_pct(min_pass_rate)} | top_fail_reasons={top_fail_reasons if top_fail_reasons else []}"
    )


class TestChatHitStabilityYamlFlow(unittest.TestCase):
    """unittest 测试套件：命中稳定性 YAML 用例。

    职责边界：
    - `setUpClass`：加载稳定性套件环境并登录。
    - `setUp/tearDown`：为每条稳定性 case 创建和释放 HTTP client。
    - `tearDownClass`：输出整套稳定性汇总。
    - 动态生成的 `test_*` 方法：执行一条需要重复运行的稳定性业务场景。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """套件级准备：清空汇总状态、读取配置并复用一次登录结果。"""
        _SUITE_CASE_SUMMARIES.clear()
        cls.stability_runtime = load_context_runtime(_STABILITY_SUITE["target_env"])
        _safe_print(
            f"STABILITY_RUNTIME target_env={cls.stability_runtime['target_env']} "
            f"auth_mode={cls.stability_runtime['auth_mode']} "
            f"base_url={cls.stability_runtime['api_base_url']} "
            f"cases_file={_STABILITY_SUITE['cases_file']}"
        )
        cls.stability_access_token = _create_stability_access_token(cls.stability_runtime)

    @classmethod
    def tearDownClass(cls) -> None:
        """套件级清理：输出所有稳定性 case 的汇总命中率。"""
        _emit_stability_suite_summary()

    def setUp(self) -> None:
        """用例级准备：为当前稳定性 YAML case 创建隔离的认证 API 客户端。"""
        self.client = create_http_client(
            base_url=self.stability_runtime["api_base_url"],
            default_headers=self.stability_runtime["headers"],
        )
        self.client.set_header("Authorization", _build_auth_header(self.stability_access_token))
        self.stability_authenticated_apis = {
            "chat_api": ChatAPI(client=self.client),
            "quality_inspection_api": QualityInspectionAPI(client=self.client),
            "runtime": self.stability_runtime,
            "access_token": self.stability_access_token,
        }

    def tearDown(self) -> None:
        """用例级清理：关闭当前稳定性 case 使用的 HTTP session。"""
        self.client.close()

    def _run_stability_case(self, case_data: Dict[str, Any]) -> None:
        """业务入口：执行动态绑定到 unittest 的单条稳定性 case。"""
        ChatHitStabilityScenario(self.stability_authenticated_apis).run_case(case_data)


bind_case_tests(
    TestChatHitStabilityYamlFlow,
    _STABILITY_CASES,
    _STABILITY_CASE_IDS,
    "_run_stability_case",
    "stability",
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
