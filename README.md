# AI 客服接口自动化

这个仓库使用 Python 标准库 `unittest` 运行接口自动化测试，用于验证三条核心链路：

1. `POST /api/auth/login`
2. `POST /chat/answer`
3. `GET /api/quality-inspection/user-detail`

主流程：

`登录 -> 调用 AI -> 查询质检结果 -> 匹配质检记录 -> 断言回复 / 知识 / 动作 / 等级`

## 目录结构

```text
project_root/
├── api_object/
│   ├── auth_api.py
│   ├── chat_api.py
│   └── quality_inspection_api.py
├── common/
│   └── http_client.py
├── config/
│   ├── env.yaml
│   ├── settings.py
│   └── context_runtime.py
├── data/
├── testcases/
│   ├── test_chat_yaml.py
│   ├── test_chat_context_yaml.py
│   ├── test_chat_hit_stability_yaml.py
│   └── unittest_helpers.py
├── .env
├── .env.example
├── run_tests.py
└── requirements.txt
```

## 安装与运行

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

运行全部 unittest 用例：

```powershell
python run_tests.py --env dev -v
```

运行主流程用例：

```powershell
python run_tests.py --env dev --pattern "test_chat_yaml.py" -v
```

运行上下文用例：

```powershell
python run_tests.py --env dev --pattern "test_chat_context_yaml.py" -v
```

运行稳定性用例：

```powershell
python run_tests.py --env dev --pattern "test_chat_hit_stability_yaml.py" -v
```

稳定性模块要求顺序执行，使用默认 `unittest` runner 即可，不要并发执行。

也可以直接使用标准库发现测试：

```powershell
$env:ENV="dev"
python -m unittest discover -s testcases -p "test_*.py" -v
```

## unittest 结构

当前测试代码按标准 unittest 生命周期拆分：

- `TestChatYamlFlow`：主流程测试套件，负责环境准备、登录复用、HTTP client 生命周期。
- `TestChatContextYamlFlow`：上下文测试套件，负责带历史消息的 YAML case 生命周期。
- `TestChatHitStabilityYamlFlow`：稳定性测试套件，负责重复运行 case 并输出汇总命中率。
- `ChatMainFlowScenario` / `ChatContextFlowScenario` / `ChatHitStabilityScenario`：业务场景执行器，只负责执行具体业务链路。
- `bind_case_tests`：把 YAML 中每条 case 动态绑定成 unittest 可识别的 `test_*` 方法。

## 环境规则

- 优先读取 suite YAML 顶部的 `target_env`
- `ENV` 只作为没有 `target_env` 时的兜底值
- `prod` 会被归一成 `console`
- YAML 里只保留 `target_env` 和 case 内容，不再配置 `shop_id`
- 店铺、账号、密码统一放在 `.env`
- 推荐使用环境专属键：`*_DEV` / `*_CONSOLE`

当前示例：

- `dev` 对应店铺 `585`
- `console` 对应店铺 `347`

## `.env` 示例

```env
ENV=dev
CHAT_PLATFORM=tmall

# dev
AI_BASE_URL_DEV=https://dev.zhiyan.chat
LOGIN_ACCOUNT_DEV=...
LOGIN_PASSWORD_DEV=...
CHAT_ACCOUNT_DEV=...
CHAT_SHOP_ID_DEV=585
CHAT_SHOP_NAME_DEV=...

# console
AI_BASE_URL_CONSOLE=https://console.zhiyan.chat
LOGIN_ACCOUNT_CONSOLE=...
LOGIN_PASSWORD_CONSOLE=...
CHAT_ACCOUNT_CONSOLE=...
CHAT_SHOP_ID_CONSOLE=347
CHAT_SHOP_NAME_CONSOLE=shop_347
ACCESS_TOKEN_CONSOLE=
```

## YAML 结构

`data/` 下的 case 文件只需要声明 `target_env`，业务运行配置由 `.env` 读取。

```yaml
target_env: "dev"

cases:
  - name: "scene_keyword_hit_greeting"
    turns:
      - question: "你好"
        expect:
          reply_contains:
            - "宝贝"
```

`cases` 支持的写法包括：

- `turns`
- `request`
- `questions`
