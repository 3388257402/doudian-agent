"""`backend/tests/agent_core/` 是一个包。

加这个文件是为了让用例之间用**相对导入**共享 `conftest.py` 里的假 client 与断言工具：

- 没有 `__init__.py` 时，`from .conftest import ...` 会 `ImportError: attempted relative
  import with no known parent package`（pytest 的 prepend importmode 把 `tests/agent_core`
  直接塞进 `sys.path`，模块名是 `test_xxx` 而不是 `agent_core.test_xxx`）；
- 加上它之后，pytest 以 `agent_core.test_graph_paths` 的名字导入用例，`.conftest` 就是
  `agent_core.conftest`，同一个模块对象。

刻意**不**加 `backend/tests/__init__.py`：那会把 `tests` 变成包并改变 pytest 的模块命名，
而 `backend/tests/` 下其它目录（platform/qa 的用例）不归本任务管。
"""
