from typing import Any, Dict, Optional

from common.http_client import create_http_client


class AuthAPI:
    def __init__(self, client=None) -> None:
        self.http_client = client or create_http_client()
        self.access_token: Optional[str] = None

    def login(self, account: str, password: str) -> Dict[str, Any]:
        response = self.http_client.post(
            "/api/auth/login",
            json={
                "account": account,
                "password": password,
            },
        )
        data = response.json()
        self.access_token = data.get("data", {}).get("accessToken")
        return {
            "status_code": response.status_code,
            "data": data,
            "access_token": self.access_token,
        }

    def get_access_token(self) -> Optional[str]:
        return self.access_token


auth_api = AuthAPI()
