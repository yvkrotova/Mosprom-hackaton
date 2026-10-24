"""Операции графа ONNX-модели — для сверки со списком поддерживаемых в SDK LinQ.

    linq/.venv-tools/Scripts/python linq/check_onnx_ops.py [--model backend/models/yolov8s.onnx]
                                                            [--supported linq/supported_ops.txt]

Печатает и сохраняет в linq/reports/onnx_ops.{json,md}: opset, входы/выходы с формами
(статичны ли), операции и их количество, хвост графа после последних свёрток (декодирование
DFL/Concat — кандидат на перенос на CPU, если SDK его не поддерживает).

--supported — текстовый файл, по одной операции на строку, выписанный ИЗ ДОКУМЕНТАЦИИ SDK
(tpu_framework.pdf). Пока его нет, неподдерживаемые операции не определяются — это честно
помечается в отчёте. Список не угадывается.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import onnx

ROOT = Path(__file__).resolve().parent.parent
REPORTS = Path(__file__).resolve().parent / "reports"


def shape(value_info) -> list:
    dims = value_info.type.tensor_type.shape.dim
    return [d.dim_value if d.HasField("dim_value") else (d.dim_param or "?") for d in dims]


def tail_after_last_conv(graph) -> list[str]:
    """Операции после последней Conv в топологическом порядке — «голова» декодирования."""
    nodes = list(graph.node)
    last_conv = max((i for i, n in enumerate(nodes) if n.op_type == "Conv"), default=-1)
    return [n.op_type for n in nodes[last_conv + 1 :]]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--model", type=Path, default=ROOT / "backend" / "models" / "yolov8s.onnx")
    parser.add_argument(
        "--supported", type=Path, default=Path(__file__).resolve().parent / "supported_ops.txt"
    )
    args = parser.parse_args()

    model = onnx.load(str(args.model))
    graph = model.graph
    ops = Counter(n.op_type for n in graph.node)
    supported = None
    if args.supported.is_file():
        supported = {
            line.strip() for line in args.supported.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }  # fmt: skip
    report = {
        "model": str(args.model.relative_to(ROOT)).replace("\\", "/"),
        "opset": {o.domain or "ai.onnx": o.version for o in model.opset_import},
        "inputs": {i.name: shape(i) for i in graph.input},
        "outputs": {o.name: shape(o) for o in graph.output},
        "static_shapes": all(
            isinstance(d, int) for v in [*graph.input, *graph.output] for d in shape(v)
        ),
        "nodes": len(graph.node),
        "ops": dict(ops.most_common()),
        "tail_after_last_conv": tail_after_last_conv(graph),
        "unsupported": sorted(set(ops) - supported) if supported is not None else None,
        "supported_list": str(args.supported.name)
        if supported is not None
        else "НЕТ — нужен список из tpu_framework.pdf",
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "onnx_ops.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    unsupported = (
        report["unsupported"]
        if supported is not None
        else "не определены (нет списка из документации)"
    )
    lines = [
        f"# Операции {report['model']}",
        "",
        f"- opset: {report['opset']}",
        f"- входы: {report['inputs']}; выходы: {report['outputs']}",
        f"- формы статичные: {report['static_shapes']}",
        f"- узлов: {report['nodes']}",
        f"- неподдерживаемые SDK: {unsupported}",
        "",
        "| Операция | Кол-во |",
        "|---|---|",
        *[f"| {op} | {n} |" for op, n in ops.most_common()],
        "",
        "Хвост после последней Conv (декодирование DFL и сборка выхода): "
        + " → ".join(report["tail_after_last_conv"]),
    ]
    (REPORTS / "onnx_ops.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
