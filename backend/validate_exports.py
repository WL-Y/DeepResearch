"""Produce local export fixtures for visual QA without search or model calls."""
import argparse
from pathlib import Path
import sys


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('--output-dir', default='workspace/export-validation')
    cli.add_argument('--input', help='Optional report Markdown to validate')
    cli.add_argument('--dependency-path', help='Optional extra package path for the QA runtime')
    args = cli.parse_args()
    if args.dependency_path:
        sys.path.append(args.dependency_path)
    from src.services.exporting import export_report

    content = Path(args.input).read_text(encoding='utf-8') if args.input else '''# 机器人决策研究报告

## 执行摘要

本报告比较方法 A 与方法 B 在相同实验协议下的结果。成功率由 35% 提高到 65%，绝对差为 30 个百分点。该结果来自 20 次试验，不能直接解释为长期部署可靠性，也不代表已经完成独立复现。

## 方法对照

| 方法 | 成功率 | 实验条件 |
| --- | --- | --- |
| 方法 A | 65% | 20 次试验 |
| 方法 B | 35% | 相同实验协议 |

表格用于对照原始结果。解释指标时需要保留样本数、评价对象和实验条件，区分作者自报结果与独立验证。

## 验证步骤

1. 固定任务、输入模态和成功判据。
   - 保留随机种子、样本数与失败记录。
2. 重复实验并说明不确定性。

```python
baseline = 7 / 20
result = 13 / 20
percentage_points = (result - baseline) * 100
```

## 局限与结论

本次证据支持在指定协议下进行比较，不支持推断任意场景中的性能。下一步应核验评价条件，并通过独立重复实验检查结果的稳定性。

## 核心来源

- [实验协议示例](https://example.org/protocol?version=2&lang=zh)
'''
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for extension in ('md', 'txt', 'html', 'docx', 'pdf'):
        path = output / f'report.{extension}'
        path.write_bytes(export_report('机器人决策研究报告', content, extension))
        print(path.resolve())


if __name__ == '__main__':
    main()
