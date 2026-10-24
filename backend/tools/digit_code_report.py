"""离线分析教师页采样：描述分布，并以留出样本检验简单递推/时间线性候选。"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def analyze(data: dict) -> dict:
    observations = data.get("observations", [])
    if not observations:
        raise ValueError("没有采样数据")
    changes = []
    last_time = -1
    for row in observations:
        code, timestamp = row.get("code"), row.get("timestampMs")
        if not isinstance(code, str) or len(code) != 4 or not code.isascii() or not code.isdigit():
            raise ValueError("码值必须为保留前导零的四位字符串")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp < last_time:
            raise ValueError("时间戳缺失或逆序")
        last_time = timestamp
        if not changes or code != changes[-1]["code"]:
            changes.append(row)
    values = [int(row["code"]) for row in changes]
    intervals = [round((b["timestampMs"] - a["timestampMs"]) / 1000, 3) for a, b in zip(changes, changes[1:])]
    result = {
        "observations": len(observations), "displayedRuns": len(changes), "uniqueCodes": len(set(values)),
        "leadingZeroRuns": sum(row["code"].startswith("0") for row in changes),
        "observedChangeIntervalsSeconds": dict(Counter(intervals).most_common(10)),
        "perPositionDigits": [dict(Counter(row["code"][position] for row in changes)) for position in range(4)],
        "commonDifferencesModulo10000": Counter((b - a) % 10000 for a, b in zip(values, values[1:])).most_common(5),
        "candidates": [],
        "limits": "连续显示相同不代表未刷新；时间为本机观察时间，可能有延迟或漏采。拟合不能证明真实算法，未拟合也不能证明密码学安全。",
    }
    if data.get("source") == "teacher-detail-api":
        result["source"] = "teacher-detail-api"
        result["limits"] = "每次只读取当前服务端码；轮询可能漏过中间刷新，同值不能区分复用或碰巧重复，时间为本机收到响应的时间。拟合不能证明真实算法，未拟合也不能证明密码学安全。"
    if len(values) < 30:
        result["candidateCheck"] = "显示值不足 30 个，不做候选拟合"
        return result
    split = max(20, int(len(values) * 0.65))
    split = min(split, len(values) - 10)

    def fit(name: str, inputs: list[int], outputs: list[int], train: int) -> None:
        # 只在本机枚举 10000 个线性系数，不连接网络、不尝试提交任何签到码。
        tested = []
        for multiplier in range(10000):
            increment = (outputs[0] - multiplier * inputs[0]) % 10000
            if all((multiplier * x + increment) % 10000 == y for x, y in zip(inputs[:train], outputs[:train])):
                hits = sum((multiplier * x + increment) % 10000 == y for x, y in zip(inputs[train:], outputs[train:]))
                tested.append({"multiplier": multiplier, "increment": increment, "holdoutHits": hits,
                               "holdoutTotal": len(outputs) - train})
        result["candidates"].append({"family": name, "trainingPairs": train, "trainingMatches": len(tested),
                                     "validatedMatches": sum(item["holdoutHits"] == item["holdoutTotal"] for item in tested),
                                     "matches": tested[:20], "outputLimit": 20})

    fit("next=(a*previous+c) mod 10000", values[:-1], values[1:], split - 1)
    fit("code=(a*unixSeconds+c) mod 10000", [int(row["timestampMs"] // 1000) for row in changes], values, split)
    fit("code=(a*fiveSecondBucket+c) mod 10000", [int(row["timestampMs"] // 5000) for row in changes], values, split)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(json.loads(args.samples.read_text(encoding="utf-8-sig")))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
