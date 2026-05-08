import os
import random
import re
import time
import unittest
from typing import Any, Dict, List

import yaml

from api_object.auth_api import AuthAPI
from api_object.chat_api import ChatAPI
from api_object.quality_inspection_api import QualityInspectionAPI
from common.http_client import create_http_client
from config.context_runtime import load_context_runtime
from config.project_env import resolve_effective_env
from testcases.unittest_helpers import bind_case_tests


def _load_context_suite() -> Dict[str, Any]:
    """加载上下文 YAML 套件，确定目标环境和待执行业务 case 列表。"""
    cases_file = os.getenv("CHAT_CONTEXT_CASES_FILE", "test_chat/test_20260501_2.yaml")
    if os.path.isabs(cases_file):
        data_path = cases_file
    else:
        data_path = os.path.join(os.path.dirname(__file__), "..", "data", cases_file)
    with open(data_path, "r", encoding="utf-8") as file:
        suite = yaml.safe_load(file) or {}

    target_env = resolve_effective_env(str(suite.get("target_env", "")).strip().lower() or os.getenv("ENV", "dev"))
    if target_env not in {"dev", "console"}:
        raise ValueError(f"context suite target_env must be dev or console, got: {suite.get('target_env')}")

    cases = suite.get("cases") or []
    if not isinstance(cases, list):
        raise ValueError("context suite cases must be a list")

    return {
        "target_env": target_env,
        "cases_file": data_path,
        "cases": cases,
    }


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("gbk", errors="backslashreplace").decode("gbk"))


def build_runtime_username(case_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", case_name.lower()).strip("_")
    short_name = normalized[:12] or "case"
    timestamp_ms = int(time.time() * 1000)
    rand4 = f"{random.getrandbits(16):04x}"
    return f"ctx_{short_name}_{timestamp_ms}_{rand4}"[:40]


def _normalize_expect(expect_data: Any) -> Dict[str, Any]:
    """归一化上下文 case 的期望断言结构。"""
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
    turn_index: int,
):
    """查询并匹配上下文场景中单轮对话对应的质检记录。"""
    selected_record = None
    quality_response = None
    for attempt in range(1, 11):
        query_start = int(turn_send_at) - 5
        query_end = int(turn_response_at) + 30 + (attempt * 2)
        _safe_print(
            f"TURN_QUALITY_WINDOW case_name={case_name} runtime_username={runtime_username} "
            f"turn_index={turn_index} startTime={query_start} endTime={query_end}"
        )
        quality_response = quality_client.get_user_detail(
            username=runtime_username,
            shop_id=shop_id,
            start_time=query_start,
            end_time=query_end,
        )
        _safe_print(
            f"TURN_QUALITY_QUERY case_name={case_name} runtime_username={runtime_username} "
            f"turn_index={turn_index} attempt={attempt} url=/api/quality-inspection/user-detail "
            f"params={quality_response['params']}"
        )
        _safe_print(
            f"TURN_QUALITY_RESPONSE case_name={case_name} runtime_username={runtime_username} "
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


def _normalize_context_case(case_data: Dict[str, Any]) -> Dict[str, Any]:
    """归一化上下文 case，把历史消息和待测轮次转换成统一业务结构。"""
    if not isinstance(case_data, dict):
        raise ValueError(f"context case must be a dict, got: {type(case_data).__name__}")

    case_name = str(case_data.get("name", "")).strip()
    if not case_name:
        raise ValueError("context case name is required")

    raw_context_messages = case_data.get("context_messages") or []
    if not isinstance(raw_context_messages, list):
        raise ValueError(f"[{case_name}] context_messages must be a list")

    context_messages: List[Dict[str, str]] = []
    for index, message in enumerate(raw_context_messages, start=1):
        if not isinstance(message, dict):
            raise ValueError(f"[{case_name}] context_messages[{index}] must be a dict")
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", "")).strip()
        if role not in {"user", "assistant"}:
            raise ValueError(f"[{case_name}] context_messages[{index}].role must be user or assistant")
        if not content:
            raise ValueError(f"[{case_name}] context_messages[{index}].content cannot be empty")
        context_messages.append({"role": role, "content": content})

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

    return {
        "name": case_name,
        "context_messages": context_messages,
        "turns": turns,
    }


def _prepare_context_messages(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """为历史上下文消息补充 created_at，模拟真实会话时间线。"""
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


_CONTEXT_SUITE = _load_context_suite()
_CONTEXT_CASE_IDS = [
    case.get("name", f"context_case_{index}")
    if isinstance(case, dict)
    else f"context_case_{index}"
    for index, case in enumerate(_CONTEXT_SUITE["cases"], start=1)
]
_CONTEXT_CASES = _CONTEXT_SUITE["cases"]


def _create_context_access_token(context_runtime: Dict[str, Any]) -> str:
    """认证动作：根据上下文流程运行环境获取访问令牌。"""
    if context_runtime["auth_mode"] == "token":
        return context_runtime["access_token"]

    auth_client_http = create_http_client(
        base_url=context_runtime["api_base_url"],
        default_headers=context_runtime["headers"],
    )
    auth_client = AuthAPI(client=auth_client_http)
    try:
        _safe_print(
            f"CONTEXT_LOGIN_REQUEST env={context_runtime['target_env']} "
            f"account={context_runtime['login_account']}"
        )
        login_response = auth_client.login(
            context_runtime["login_account"],
            context_runtime["login_password"],
        )
        _safe_print(
            f"CONTEXT_LOGIN_RESPONSE env={context_runtime['target_env']} "
            f"status={login_response['status_code']} body={login_response['data']}"
        )
        assert login_response["status_code"] == 200
        assert login_response["data"].get("code") == 200
        access_token = login_response.get("access_token")
        assert access_token, "context login succeeded but accessToken was empty"
        return access_token
    finally:
        auth_client_http.close()


class ChatContextFlowScenario:
    """业务场景执行器：带历史上下文的 `聊天 -> 质检查询 -> 结果断言`。

    该对象负责把 YAML 中的 `context_messages` 和 `turns` 转成接口请求，并完成每轮断言。
    unittest 测试类只负责生命周期管理。
    """

    def __init__(self, authenticated_apis: Dict[str, Any]) -> None:
        """保存上下文业务场景需要使用的 API 对象和运行态配置。"""
        self.authenticated_apis = authenticated_apis

    def run_case(self, case_data: Dict[str, Any]) -> None:
        """执行单条上下文 YAML case。"""
        _run_chat_context_flow(self.authenticated_apis, case_data)


def _run_chat_context_flow(context_authenticated_apis: Dict[str, Any], case_data: Dict[str, Any]) -> None:
    """上下文业务流程：带历史消息发起聊天、查询质检记录，并完成断言。"""
    chat_client: ChatAPI = context_authenticated_apis["chat_api"]
    quality_client: QualityInspectionAPI = context_authenticated_apis["quality_inspection_api"]
    runtime = context_authenticated_apis["runtime"]
    normalized_case = _normalize_context_case(case_data)
    case_name = normalized_case["name"]
    shop_id = runtime["shop_id"]
    turns = normalized_case["turns"]
    runtime_username = build_runtime_username(case_name)
    case_label = f"{case_name}|username={runtime_username}"
    shop_name = runtime["shop_name"]

    _safe_print(
        f"CONTEXT_CASE case_name={case_name} runtime_username={runtime_username} "
        f"shop_id={shop_id} target_env={runtime['target_env']} "
        f"context_count={len(normalized_case['context_messages'])} turns_count={len(turns)}"
    )

    conversation_messages = _prepare_context_messages(normalized_case["context_messages"])
    turn_failures: List[str] = []

    for turn_index, turn in enumerate(turns, start=1):
        question = turn["question"]
        turn_expect = _normalize_expect(turn.get("expect", {}))
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
                f"TURN_CONTEXT case_name={case_name} runtime_username={runtime_username} "
                f"turn_index={turn_index} question={question} request_messages_count={len(conversation_messages)}"
            )

            chat_response = chat_client.chat_answer(
                account=runtime["chat_account"],
                messages=list(conversation_messages),
                platform=runtime["platform"],
                shop_id=shop_id,
                shop_name=shop_name,
                username=runtime_username,
                is_test=runtime["is_test"],
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
            _safe_print(
                f"TURN_RESULT case_name={case_name} runtime_username={runtime_username} "
                f"turn_index={turn_index} assistant_messages_count={len(assistant_messages)}"
            )
            assert assistant_messages, f"[{case_label}] turn {turn_index} returned no assistant messages"
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
                turn_index=turn_index,
            )
            assert quality_response is not None
            assert selected_record is not None, f"[{case_label}] turn {turn_index} did not match quality record"

            normalized_turn_record = quality_client.normalize_quality_record(selected_record)
            final_response_text = normalized_turn_record["final_reply"]
            turn_summary = {
                "level": normalized_turn_record["level"],
                "categories": normalized_turn_record["categories"],
                "final_reply": final_response_text,
            }
            _safe_print(
                f"TURN_QUALITY_MATCHED case_name={case_name} runtime_username={runtime_username} "
                f"turn_index={turn_index} summary={turn_summary}"
            )
            _safe_print(f"TURN_{turn_index}_EXTRACTED_STATS {normalized_turn_record['stats_map']}")
            _safe_print(f"TURN_{turn_index}_EXTRACTED_DETAILS {normalized_turn_record['details_map']}")

            assert final_response_text, f"[{case_label}] turn {turn_index} quality final_response was empty"
            assert final_response_text == chat_reply, (
                f"[{case_label}] turn {turn_index} chat reply and quality final_reply mismatch | "
                f"chat: {chat_reply} | quality: {final_response_text}"
            )

            if turn_expect:
                _assert_reply(case_label, turn_expect, chat_reply, final_response_text)
                _assert_quality(case_label, turn_expect, normalized_turn_record)
                _assert_stats(case_label, turn_expect, normalized_turn_record)
                _assert_details(case_label, turn_expect, normalized_turn_record)
                _assert_actions(case_label, turn_expect, normalized_turn_record)
                _safe_print(
                    f"TURN_ASSERT_RESULT case_name={case_name} turn_index={turn_index} asserted=true result=PASS"
                )
            else:
                _safe_print(
                    f"TURN_ASSERT_RESULT case_name={case_name} turn_index={turn_index} asserted=false result=SKIP"
                )
        except AssertionError as exc:
            failure = f"turn={turn_index} question={question} error={exc}"
            turn_failures.append(failure)
            _safe_print(f"TURN_ASSERT_RESULT case_name={case_name} turn_index={turn_index} result=FAIL detail={failure}")
        except Exception as exc:  # pragma: no cover
            failure = f"turn={turn_index} question={question} exception={type(exc).__name__}: {exc}"
            turn_failures.append(failure)
            _safe_print(f"TURN_ASSERT_RESULT case_name={case_name} turn_index={turn_index} result=ERROR detail={failure}")

        if turn_index < len(turns):
            _safe_print(
                f"TURN_WAIT case_name={case_name} runtime_username={runtime_username} "
                f"turn_index={turn_index} sleep_seconds=1"
            )
            time.sleep(1)

    assert not turn_failures, (
        f"[{case_label}] context turn assertions failed, total {len(turn_failures)} turns\n"
        + "\n".join(turn_failures)
    )


class TestChatContextYamlFlow(unittest.TestCase):
    """unittest 测试套件：上下文 YAML 用例。

    职责边界：
    - `setUpClass`：加载上下文套件环境并登录。
    - `setUp/tearDown`：为每条上下文 case 创建和释放 HTTP client。
    - 动态生成的 `test_*` 方法：执行一条带历史消息的业务场景。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """套件级准备：读取上下文流程配置并复用一次登录结果。"""
        cls.context_runtime = load_context_runtime(_CONTEXT_SUITE["target_env"])
        _safe_print(
            f"CONTEXT_RUNTIME target_env={cls.context_runtime['target_env']} "
            f"auth_mode={cls.context_runtime['auth_mode']} "
            f"base_url={cls.context_runtime['api_base_url']}"
        )
        cls.context_access_token = _create_context_access_token(cls.context_runtime)

    def setUp(self) -> None:
        """用例级准备：为当前上下文 YAML case 创建隔离的认证 API 客户端。"""
        self.client = create_http_client(
            base_url=self.context_runtime["api_base_url"],
            default_headers=self.context_runtime["headers"],
        )
        self.client.set_header("Authorization", _build_auth_header(self.context_access_token))
        self.context_authenticated_apis = {
            "chat_api": ChatAPI(client=self.client),
            "quality_inspection_api": QualityInspectionAPI(client=self.client),
            "runtime": self.context_runtime,
            "access_token": self.context_access_token,
        }

    def tearDown(self) -> None:
        """用例级清理：关闭当前上下文 case 使用的 HTTP session。"""
        self.client.close()

    def _run_context_case(self, case_data: Dict[str, Any]) -> None:
        """业务入口：执行动态绑定到 unittest 的单条上下文 case。"""
        ChatContextFlowScenario(self.context_authenticated_apis).run_case(case_data)


bind_case_tests(
    TestChatContextYamlFlow,
    _CONTEXT_CASES,
    _CONTEXT_CASE_IDS,
    "_run_context_case",
    "context",
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
