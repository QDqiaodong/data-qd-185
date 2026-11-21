import argparse
import json
from .campaign import run_campaign
from .io import load, publish
from .service import run


def main():
    parser = argparse.ArgumentParser(description="数字电路测试向量回放")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", help="v1 顺序回放输入 JSON")
    mode.add_argument("--campaign", help="v2 故障覆盖排程输入 JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.campaign is not None:
            result = run_campaign(load(args.campaign))
        else:
            result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"logic-replay: {exc}\n")


if __name__ == "__main__":
    main()
