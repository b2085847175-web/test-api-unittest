from typing import Any, Dict, List, Optional

from common.http_client import http_client


class QualityInspectionAPI:
    def __init__(self, client=None) -> None:
        self.http_client = client or http_client

    def get_user_detail(
        self,
        username: str,
        shop_id: str,
        start_time: Any,
        end_time: Any,
        level: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "username": username,
            "shop_id": shop_id,
            "startTime": start_time,
            "endTime": end_time,
        }
        if level:
            params["level"] = level
        if category:
            params["category"] = category

        response = self.http_client.request("GET", "/api/quality-inspection/user-detail", params=params)
        data = response.json()
        return {
            "status_code": response.status_code,
            "params": params,
            "data": data,
            "code": data.get("code"),
            "msg": data.get("msg"),
            "records": data.get("data", {}).get("records", []) if isinstance(data.get("data"), dict) else [],
        }

    @staticmethod
    def extract_final_response_text(record: Dict[str, Any]) -> Optional[str]:
        actions = record.get("final_response", {}).get("ai_actions", [])
        for action in actions:
            if action.get("actionType") != "sendMessage":
                continue
            payload = action.get("payload", {})
            if payload.get("contentType") == "text":
                return payload.get("content")
        return None

    @staticmethod
    def _append_unique(target: Dict[str, List[str]], key: str, value: str) -> None:
        if not value:
            return
        target.setdefault(key, [])
        if value not in target[key]:
            target[key].append(value)

    def normalize_quality_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        stats_map: Dict[str, List[str]] = {}
        for item in record.get("stats", []):
            stat_key = item.get("key")
            stat_value = item.get("value", {})
            if not stat_key:
                continue
            if isinstance(stat_value, dict):
                name = stat_value.get("name")
                if name:
                    self._append_unique(stats_map, stat_key, str(name))
            elif stat_value:
                self._append_unique(stats_map, stat_key, str(stat_value))

        details_map: Dict[str, List[str]] = {}
        for detail in record.get("details", []):
            detail_key = str(detail.get("key", "")).strip()
            detail_message = str(detail.get("message", "")).strip()
            if not detail_key:
                continue

            self._append_unique(details_map, detail_key, detail_message)

            if detail_key == "店铺知识库":
                if "AI理解意图：" in detail_message:
                    intent_part = detail_message.split("AI理解意图：", 1)[1]
                    intent_part = intent_part.split("知识内容：", 1)[0]
                    for line in intent_part.splitlines():
                        line = line.strip()
                        if line:
                            self._append_unique(details_map, "AI理解意图", line)
                if "知识内容：" in detail_message:
                    knowledge_part = detail_message.split("知识内容：", 1)[1]
                    for line in knowledge_part.splitlines():
                        line = line.strip()
                        if line:
                            self._append_unique(details_map, "店铺知识库内容", line)

            if detail_key == "货品知识库":
                self._append_unique(details_map, "商品信息", detail_message)

        # Some quality records surface scene hits only in details["场景知识库"].
        # Backfill stats.scene_knowledge so existing YAML expectations continue to work.
        for detail_scene in details_map.get("场景知识库", []):
            for scene_name in str(detail_scene).splitlines():
                scene_name = scene_name.strip()
                if scene_name:
                    self._append_unique(stats_map, "scene_knowledge", scene_name)

        actions = record.get("final_response", {}).get("ai_actions", [])
        action_types: List[str] = []
        forward_scenes: List[str] = []
        for action in actions:
            action_type = action.get("actionType")
            if action_type and action_type not in action_types:
                action_types.append(action_type)
            payload = action.get("payload", {})
            scene = payload.get("scene")
            if scene and scene not in forward_scenes:
                forward_scenes.append(scene)

        if not forward_scenes:
            for scene in details_map.get("转接场景", []):
                if scene not in forward_scenes:
                    forward_scenes.append(scene)

        return {
            "level": record.get("level"),
            "categories": record.get("categories", []),
            "stats_map": stats_map,
            "details_map": details_map,
            "action_types": action_types,
            "forward_scenes": forward_scenes,
            "final_reply": self.extract_final_response_text(record),
            "raw_record": record,
        }

    def find_best_matching_record(
        self,
        records: List[Dict[str, Any]],
        username: str,
        shop_id: str,
        start_time: float,
        end_time: float,
        chat_reply: str,
        chat_response: Optional[Dict[str, Any]] = None,
        user_message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        当前 chat 与 quality 没有稳定的一对一关联键。
        因此优先尝试唯一字段，失败后再退化为 username/shop/time + 回复文本匹配。
        """
        unique_values: Dict[str, Any] = {}
        if chat_response:
            response_body = chat_response.get("data", {})
            data_body = response_body.get("data", {}) if isinstance(response_body, dict) else {}
            if response_body.get("request_id"):
                unique_values["request_id"] = response_body.get("request_id")
            if data_body.get("user_message_id"):
                unique_values["user_message_id"] = data_body.get("user_message_id")
            if data_body.get("session_id"):
                unique_values["session_id"] = data_body.get("session_id")

        unique_matches = []
        for record in records:
            for key, value in unique_values.items():
                if value is not None and record.get(key) == value:
                    unique_matches.append(record)
                    break
        if unique_matches:
            return max(unique_matches, key=lambda item: float(item.get("end_time", 0)))

        candidates = []
        for record in records:
            if str(record.get("username")) != str(username):
                continue
            if str(record.get("shop_id")) != str(shop_id):
                continue
            record_start = float(record.get("start_time", 0))
            record_end = float(record.get("end_time", 0))
            if record_start < start_time - 10 or record_end > end_time + 20:
                continue
            candidates.append(record)

        if not candidates:
            return None

        exact_reply_matches = []
        for record in candidates:
            final_text = self.extract_final_response_text(record)
            if final_text == chat_reply:
                exact_reply_matches.append(record)

        if exact_reply_matches:
            return max(exact_reply_matches, key=lambda item: float(item.get("end_time", 0)))

        if user_message:
            message_related = []
            for record in candidates:
                normalized = self.normalize_quality_record(record)
                if any(user_message in item for values in normalized["details_map"].values() for item in values):
                    message_related.append(record)
            if message_related:
                return max(message_related, key=lambda item: float(item.get("end_time", 0)))

        return max(candidates, key=lambda item: float(item.get("end_time", 0)))


quality_inspection_api = QualityInspectionAPI()
