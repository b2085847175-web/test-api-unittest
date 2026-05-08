import argparse
import os
import sys
import unittest
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析 unittest 命令行参数，兼容原来按环境和文件筛选的执行习惯。"""
    parser = argparse.ArgumentParser(description="Run API tests with unittest.")
    parser.add_argument("--env", default=None, help="Test environment, for example dev or console.")
    parser.add_argument("--start-dir", default="testcases", help="Directory used by unittest discovery.")
    parser.add_argument("--pattern", default="test_*.py", help="Discovery filename pattern.")
    parser.add_argument("--top-level-dir", default=None, help="Project root for unittest discovery.")
    parser.add_argument("--failfast", action="store_true", help="Stop after the first failure.")
    parser.add_argument("--buffer", action="store_true", help="Buffer stdout/stderr during test execution.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Use quiet output.")
    parser.add_argument("-v", "--verbose", action="count", default=1, help="Increase output verbosity.")
    return parser.parse_args()


def main() -> int:
    """unittest 命令行入口：设置环境变量、发现测试、执行并返回进程退出码。"""
    args = parse_args()
    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if args.env:
        os.environ["ENV"] = args.env

    top_level_dir = args.top_level_dir or str(project_root)
    loader = unittest.defaultTestLoader
    suite = loader.discover(
        start_dir=args.start_dir,
        pattern=args.pattern,
        top_level_dir=top_level_dir,
    )

    verbosity = 0 if args.quiet else max(args.verbose, 1)
    runner = unittest.TextTestRunner(
        verbosity=verbosity,
        failfast=args.failfast,
        buffer=args.buffer,
    )
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
