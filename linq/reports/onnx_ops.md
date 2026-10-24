# Операции backend/models/yolov8s.onnx

- opset: {'ai.onnx': 12}
- входы: {'images': [1, 3, 640, 640]}; выходы: {'output0': [1, 84, 8400]}; формы статичные: True
- узлов: 234
- неподдерживаемые SDK: не определены (нет списка из документации)

| Операция | Кол-во |
|---|---|
| Conv | 64 |
| Sigmoid | 58 |
| Mul | 58 |
| Concat | 17 |
| Split | 8 |
| Add | 8 |
| Reshape | 8 |
| MaxPool | 3 |
| Resize | 2 |
| Transpose | 2 |
| Slice | 2 |
| Sub | 2 |
| Softmax | 1 |
| Div | 1 |

Хвост после последней Conv (декодирование DFL и сборка выхода): Reshape → Slice → Slice → Sub → Add → Add → Sub → Div → Concat → Mul → Concat
