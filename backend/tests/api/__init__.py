"""`tests/api/` 是一个包：让用例能用 `from .conftest import ...` 显式拿到断言工具
（conftest 不应被测试模块 import 的规矩在这里放宽一次，理由与 `tests/agent_core` 相同：
工具函数比"每个文件各抄一遍"更不容易走散）。"""
