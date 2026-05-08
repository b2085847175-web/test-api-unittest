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


def _load_suite() -> Dict[str, Any]:
    """加载主流程 YAML 套件，确定目标环境和待执行业务 case 列表。"""
    cases_file = os.getenv("CHAT_CASES_FILE", "test_chat.yaml")
    if os.path.isabs(cases_file):
        data_path = cases_file
    else:
        data_path = os.path.join(os.path.dirname(__file__), "..", "data", cases_file)
    with open(data_path, "r", encoding="utf-8") as file:
        suite = yaml.safe_load(file) or {}

    target_env = resolve_effective_env(str(suite.get("target_env", "")).strip().lower() or os.getenv("ENV", "dev"))
    if target_env not in {"dev", "console"}:
        raise ValueError(f"chat suite target_env must be dev or console, got: {suite.get('target_env')}")

    cases = suite.get("cases") or []
    if not isinstance(cases, list):
        raise ValueError("chat suite cases must be a list")

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
    return f"at_{short_name}_{timestamp_ms}_{rand4}"[:40]


def _build_auth_header(access_token: str) -> str:
    normalized = str(access_token or "").strip()
    if not normalized:
        return normalized
    if normalized.lower().startswith("bearer "):
        return normalized
    return f"Bearer {normalized}"


_CHAT_SUITE = _load_suite()
_CHAT_CASES = _CHAT_SUITE["cases"]
_CHAT_CASE_IDS = [
    case.get("name", f"chat_case_{index}")
    if isinstance(case, dict)
    else f"chat_case_{index}"
    for index, case in enumerate(_CHAT_CASES, start=1)
]


def _normalize_case_input(case_data: Dict[str, Any]) -> Dict[str, Any]:
    """归一化主流程 case，把 `turns/request/questions` 统一成多轮对话结构。"""
    expect_data = _normalize_expect(case_data.get("expect", {}))
    if "turns" in case_data:
        turns: List[Dict[str, Any]] = []
        for turn in case_data.get("turns", []):
            if isinstance(turn, str):
                turns.append({"question": turn, "expect": {}})
                continue
            if not isinstance(turn, dict):
                continue
            question = turn.get("question") or turn.get("message")
            if not question:
                continue
            turns.append(
                {
                    "question": question,
                    "expect": _normalize_expect(turn.get("expect", {})),
                }
            )
        return {
            "name": case_data["name"],
            "turns": turns,
        }

    if "request" in case_data:
        request_data = case_data["request"]
        questions = request_data.get("questions")
        if not questions and request_data.get("message"):
            questions = [request_data["message"]]
        turns = [{"question": question, "expect": {}} for question in (questions or [])]
        if turns and expect_data:
            turns[-1]["expect"] = expect_data
        return {
            "name": case_data["name"],
            "turns": turns,
        }

    questions = list(case_data.get("questions", []))
    turns = [{"question": question, "expect": {}} for question in questions]
    if turns and expect_data:
        turns[-1]["expect"] = expect_data
    return {
        "name": case_data["name"],
        "turns": turns,
    }


def _normalize_expect(expect_data: Any) -> Dict[str, Any]:
    # 业务断言归一化：兼容 YAML 简写，并统一映射到知识命中断言结构。
    """
    兼容简化写法：
    - expect: "活动促销和优惠"
    - expect:
        scene: "活动促销和优惠"
    统一映射到 knowledge.stats_contains.scene_knowledge。
    """
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
            f"[{case_label}] {field_name} 缺少期望值: {expected} | "
            f"实际值: {actual_list}"
        )


def _split_scene_keywords(text: str) -> List[str]:
    text = str(text or "").strip()
    if not text:
        return []
    parts = re.split(r"[、,，;；/\s和与及]+", text)
    tokens: List[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", part) and len(part) >= 4 and len(part) % 2 == 0:
            for i in range(0, len(part), 2):
                token = part[i : i + 2]
                if token:
                    tokens.append(token)
        else:
            tokens.append(part)

    deduped: List[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return deduped


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
            f"[{case_label}] chat reply 未包含期望文本: {expected_text} | "
            f"实际值: {chat_reply}"
        )
        assert expected_text in final_reply, (
            f"[{case_label}] quality final_reply 未包含期望文本: {expected_text} | "
            f"实际值: {final_reply}"
        )


def _assert_quality(case_label: str, expected: Dict[str, Any], normalized_record: Dict[str, Any]) -> None:
    quality_expect = expected.get("quality", {})
    expected_level = quality_expect.get("level")
    if expected_level:
        assert normalized_record["level"] == expected_level, (
            f"[{case_label}] level 不符合预期 | 期望: {expected_level} | "
            f"实际: {normalized_record['level']}"
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
                    f"[{case_label}] stats.scene_knowledge 未命中期望场景: {expected_scene} | "
                    f"实际值: {actual_values}"
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
                f"[{case_label}] details.{detail_key} 未命中期望文本: {expected_value} | "
                f"实际值: {actual_values}"
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
    """查询并匹配单轮对话对应的质检记录。"""
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


def _run_chat_and_quality_flow(chat_authenticated_apis: Dict[str, Any], case_data: Dict[str, Any]) -> None:
    """业务主流程：发送聊天请求、查询质检记录，并按 YAML 期望完成断言。"""
    chat_client: ChatAPI = chat_authenticated_apis["chat_api"]
    quality_client: QualityInspectionAPI = chat_authenticated_apis["quality_inspection_api"]
    runtime = chat_authenticated_apis["runtime"]
    normalized_case = _normalize_case_input(case_data)
    case_name = normalized_case["name"]
    shop_id = runtime["shop_id"]
    turns = normalized_case["turns"]
    runtime_username = build_runtime_username(case_name)
    case_label = f"{case_name}|username={runtime_username}"
    shop_name = runtime["shop_name"]
    account = runtime["chat_account"]
    platform = runtime["platform"]
    is_test = runtime["is_test"]

    assert turns, f"[{case_label}] turns 不能为空"

    _safe_print(
        f"CASE_CONTEXT case_name={case_name} runtime_username={runtime_username} "
        f"shop_id={shop_id} turns_count={len(turns)}"
    )

    conversation_messages: List[Dict[str, Any]] = []
    turn_failures: List[str] = []
    chat_response: Dict[str, Any] = {}

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
                f"shop_id={shop_id} turn_index={turn_index} question={question} "
                f"request_messages_count={len(conversation_messages)}"
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
            _safe_print(
                f"TURN_RESULT case_name={case_name} runtime_username={runtime_username} "
                f"turn_index={turn_index} assistant_messages_count={len(assistant_messages)}"
            )
            assert assistant_messages, f"[{case_label}] 第 {turn_index} 轮未提取到 assistant 回复消息"
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
            assert selected_record is not None, f"[{case_label}] 第 {turn_index} 轮未找到对应的质检记录"

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

            assert final_response_text, f"[{case_label}] 第 {turn_index} 轮未提取到质检 final_response 文本"
            assert final_response_text == chat_reply, (
                f"[{case_label}] 第 {turn_index} 轮 chat 回复与质检 final_reply 不一致 | "
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
        f"[{case_label}] 多轮断言存在失败，共 {len(turn_failures)} 轮失败:\n"
        + "\n".join(turn_failures)
    )


def _create_chat_access_token(chat_runtime: Dict[str, Any]) -> str:
    """认证动作：根据运行环境获取主流程测试需要的访问令牌。"""
    if chat_runtime["auth_mode"] == "token":
        return chat_runtime["access_token"]

    auth_client_http = create_http_client(
        base_url=chat_runtime["api_base_url"],
        default_headers=chat_runtime["headers"],
    )
    auth_client = AuthAPI(client=auth_client_http)
    try:
        _safe_print(
            f"CHAT_LOGIN_REQUEST env={chat_runtime['target_env']} "
            f"account={chat_runtime['login_account']}"
        )
        login_response = auth_client.login(
            chat_runtime["login_account"],
            chat_runtime["login_password"],
        )
        _safe_print(
            f"CHAT_LOGIN_RESPONSE env={chat_runtime['target_env']} "
            f"status={login_response['status_code']} body={login_response['data']}"
        )
        assert login_response["status_code"] == 200
        assert login_response["data"].get("code") == 200
        access_token = login_response.get("access_token")
        assert access_token, "chat login succeeded but accessToken was empty"
        return access_token
    finally:
        auth_client_http.close()


class ChatMainFlowScenario:
    """业务场景执行器：主流程 `聊天 -> 质检查询 -> 结果断言`。

    该对象只封装业务流程，不处理 unittest 生命周期。测试类负责准备环境和 HTTP client，
    业务场景对象负责消费这些资源并执行一条 YAML case。
    """

    def __init__(self, authenticated_apis: Dict[str, Any]) -> None:
        """保存本条业务场景运行所需的 API 对象和运行态配置。"""
        self.authenticated_apis = authenticated_apis

    def run_case(self, case_data: Dict[str, Any]) -> None:
        """执行单条主流程 YAML case。"""
        _run_chat_and_quality_flow(self.authenticated_apis, case_data)


class TestChatYamlFlow(unittest.TestCase):
    """unittest 测试套件：主流程 YAML 用例。

    职责边界：
    - `setUpClass`：加载环境配置并登录。
    - `setUp/tearDown`：为每条 case 创建和释放 HTTP client。
    - 动态生成的 `test_*` 方法：执行一条 YAML 业务场景。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """套件级准备：读取主流程配置并复用一次登录结果。"""
        cls.chat_runtime = load_context_runtime(_CHAT_SUITE["target_env"])
        _safe_print(
            f"CHAT_RUNTIME target_env={cls.chat_runtime['target_env']} "
            f"auth_mode={cls.chat_runtime['auth_mode']} "
            f"base_url={cls.chat_runtime['api_base_url']} "
            f"cases_file={_CHAT_SUITE['cases_file']}"
        )
        cls.chat_access_token = _create_chat_access_token(cls.chat_runtime)

    def setUp(self) -> None:
        """用例级准备：为当前 YAML case 创建隔离的认证 API 客户端。"""
        self.client = create_http_client(
            base_url=self.chat_runtime["api_base_url"],
            default_headers=self.chat_runtime["headers"],
        )
        self.client.set_header("Authorization", _build_auth_header(self.chat_access_token))
        self.chat_authenticated_apis = {
            "chat_api": ChatAPI(client=self.client),
            "quality_inspection_api": QualityInspectionAPI(client=self.client),
            "runtime": self.chat_runtime,
            "access_token": self.chat_access_token,
        }

    def tearDown(self) -> None:
        """用例级清理：关闭当前 YAML case 使用的 HTTP session。"""
        self.client.close()

    def _run_chat_case(self, case_data: Dict[str, Any]) -> None:
        """业务入口：执行动态绑定到 unittest 的单条主流程 case。"""
        ChatMainFlowScenario(self.chat_authenticated_apis).run_case(case_data)


bind_case_tests(TestChatYamlFlow, _CHAT_CASES, _CHAT_CASE_IDS, "_run_chat_case", "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
