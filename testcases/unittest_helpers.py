import re
from typing import Any, Iterable, Type


def _method_name(prefix: str, index: int, case_id: str) -> str:
    """生成 unittest 可识别的测试方法名，对应一条 YAML 业务用例。"""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", str(case_id or "")).strip("_").lower()
    if not slug:
        slug = f"case_{index}"
    if slug[0].isdigit():
        slug = f"case_{slug}"
    return f"test_{prefix}_{index:03d}_{slug[:80]}"


def bind_case_tests(
    test_case_class: Type[Any],
    cases: Iterable[Any],
    case_ids: Iterable[str],
    runner_method_name: str,
    prefix: str,
) -> None:
    """把 YAML cases 动态挂载成 TestCase 上的标准 test_* 方法。

    业务含义：
    - 一个 unittest.TestCase 类代表一个测试套件，例如主流程、上下文流程、稳定性流程。
    - YAML 里的每条 case 代表一个业务场景。
    - 每条业务场景最终都会成为一个独立的 unittest 测试方法，便于单独统计失败。
    """
    used_names: set[str] = set()

    for index, (case_data, case_id) in enumerate(zip(cases, case_ids), start=1):
        base_name = _method_name(prefix, index, case_id)
        method_name = base_name
        suffix = 2
        while method_name in used_names:
            method_name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(method_name)

        def test_method(self, case_data=case_data, case_id=case_id):
            """执行一条从 YAML 动态生成的业务场景用例。"""
            with self.subTest(case=case_id):
                getattr(self, runner_method_name)(case_data)

        test_method.__name__ = method_name
        test_method.__qualname__ = f"{test_case_class.__name__}.{method_name}"
        test_method.__doc__ = str(case_id)
        setattr(test_case_class, method_name, test_method)
