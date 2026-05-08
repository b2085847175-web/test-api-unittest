import time
import uuid
from typing import Any, Dict, List, Optional

from common.http_client import http_client
from config.settings import settings


class ChatAPI:
    def __init__(self, client=None) -> None:
        self.http_client = client or http_client
        self.base_url = settings.get_api_base_url()

    def chat_answer(
        self,
        account: str,
        messages: List[Dict[str, Any]],
        inquiry_product: Optional[Dict[str, Any]] = None,
        is_test: bool = True,
        last_order_info: Optional[Dict[str, Any]] = None,
        last_order_time: Optional[int] = None,
        platform: str = "tmall",
        request_id: Optional[str] = None,
        shop_id: str = "585",
        shop_name: str = "儒意化妆品旗舰店",
        username: str = "tb_xxx",
        **kwargs,
    ) -> Dict[str, Any]:
        prepared_messages = []
        for message in messages:
            current = dict(message)
            current.setdefault("created_at", int(time.time()))
            prepared_messages.append(current)

        payload = {
            "account": account,
            "inquiry_product": inquiry_product or {},
            "is_test": is_test,
            "last_order_info": last_order_info,
            "last_order_time": last_order_time or int(time.time()),
            "messages": prepared_messages,
            "platform": platform,
            "request_id": request_id or str(uuid.uuid4()),
            "shop_id": shop_id,
            "shop_name": shop_name,
            "username": username,
            **kwargs,
        }

        response = self.http_client.post("/chat/answer", json=payload)
        return {
            "status_code": response.status_code,
            "payload": payload,
            "data": response.json(),
        }

    @staticmethod
    def _extract_ai_actions(response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        actions = response_data.get("data", {}).get("data", {}).get("ai_actions", [])
        if not actions:
            actions = response_data.get("data", {}).get("ai_actions", [])
        return actions

    @classmethod
    def extract_ai_reply(cls, response_data: Dict[str, Any]) -> Optional[str]:
        actions = cls._extract_ai_actions(response_data)
        for action in actions:
            if action.get("actionType") != "sendMessage":
                continue
            payload = action.get("payload", {})
            if payload.get("contentType") == "text":
                return payload.get("content")
        return None

    @classmethod
    def extract_assistant_messages(
        cls,
        response_data: Dict[str, Any],
        response_received_at: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        actions = cls._extract_ai_actions(response_data)
        assistant_created_at = float(response_received_at or time.time())

        messages: List[Dict[str, Any]] = []
        for action in actions:
            if action.get("actionType") != "sendMessage":
                continue
            payload = action.get("payload", {})
            if payload.get("contentType") != "text":
                continue
            content = payload.get("content")
            if not content:
                continue
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "created_at": assistant_created_at,
                }
            )
        return messages


chat_api = ChatAPI()
