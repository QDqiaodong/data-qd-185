import argparse
import json
from .campaign import run_campaign
from .io import load, publish
from .qualify import run_qualify
from .recover import run_recover
from .service import run
from .shard import run_pack_shard, run_rollup_shards


def main():
    parser = argparse.ArgumentParser(description="数字电路测试向量回放")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", help="v1 顺序回放输入 JSON")
    mode.add_argument("--campaign", help="v2 故障覆盖排程输入 JSON")
    mode.add_argument("--qualify", help="v3 跨版本资格认定输入 JSON")
    mode.add_argument("--recover", help="v4 采集恢复与回放包导出输入 JSON")
    mode.add_argument("--pack-shard", help="v5 工位分片归档输入 JSON")
    mode.add_argument("--rollup-shards", help="v5 中心分片归并与更正清单 JSON")
    parser.add_argument("--approvals", help="v3 授权清单 JSON（仅与 --qualify 同用）")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.qualify is None and args.approvals is not None:
        parser.error("--approvals 仅与 --qualify 同用")
    if args.qualify is not None and args.approvals is None:
        parser.error("--qualify 需要 --approvals")
    try:
        if args.campaign is not None:
            result = run_campaign(load(args.campaign))
        elif args.qualify is not None:
            result = run_qualify(load(args.qualify), load(args.approvals))
        elif args.recover is not None:
            result = run_recover(load(args.recover))
        elif args.pack_shard is not None:
            result = run_pack_shard(load(args.pack_shard))
        elif args.rollup_shards is not None:
            result = run_rollup_shards(load(args.rollup_shards), args.rollup_shards)
        else:
            result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"logic-replay: {exc}\n")


if __name__ == "__main__":
    main()
