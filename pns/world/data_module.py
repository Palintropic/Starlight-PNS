# pns/world/data_module.py — 纯数据 .py 文件的严格求值器
#
# `pns/world/scenes.py` 和 `facts.py` 长得像模块，实际是数据文件。读它们的老办法是
# `exec(compile(tree, ...), {"__builtins__": {}}, ns)` —— 那个"禁用 builtins"只是
# 挡住了名字查找，AST 本身仍然被整棵执行：`while True: pass` 会挂死进程，
# `().__class__.__base__.__subclasses__()` 这类绕过 builtins 的老套路照样跑得起来。
# 而写这两个文件的接口（World Editor 的源码兜底）在整个 API 层没有鉴权。
#
# 所以这里根本不执行代码：自己走一遍 AST，只认白名单节点，遇到别的立刻报错。
# 拒绝发生在求值之前，所以无限循环这种东西连"跑一下试试"的机会都没有。
#
# 允许的东西刚好够表达这两个文件：顶层 `NAME = <字面量>` 赋值、模块文档字符串、
# 字面量常量、dict / list / tuple / set、负号，以及引用本文件里更早定义过的名字
# （`scenes.py` 里的 `LORE_TIER_INFERRED` 就是这么用的）。
#
# 明确拒绝：函数调用、属性访问、下标、import、函数/类定义、循环、分支、with、try、
# 推导式、lambda、f-string、海象、增量赋值、下标赋值、星号展开、await/yield。
import ast
from typing import Dict


class DataModuleError(ValueError):
    """数据文件里出现了不该出现的东西，或者根本没法解析。"""


# 只有这些节点能出现在值表达式里。
_ALLOWED_VALUE_NODES = (
    ast.Constant,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.UnaryOp,
    ast.Name,
)
_ALLOWED_UNARY_OPS = (ast.USub, ast.UAdd)
_ALLOWED_CONSTANTS = (str, int, float, bool, type(None))


def _describe(node: ast.AST) -> str:
    return type(node).__name__


def _reject(node: ast.AST, what: str) -> "DataModuleError":
    return DataModuleError(
        f"第 {getattr(node, 'lineno', '?')} 行：数据文件里不允许{what}"
        f"（{_describe(node)}）。这个文件只能包含字面量赋值；"
        f"需要逻辑就属于 cold update，改代码后重启。"
    )


def _eval_value(node: ast.AST, namespace: Dict):
    if not isinstance(node, _ALLOWED_VALUE_NODES):
        raise _reject(node, "这种表达式")

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, _ALLOWED_CONSTANTS):
            raise _reject(node, f"这种常量类型（{type(node.value).__name__}）")
        return node.value

    if isinstance(node, ast.Name):
        # 只认本文件里更早绑定过的名字，不给任何内建或外部名字留入口。
        if node.id not in namespace:
            raise DataModuleError(
                f"第 {node.lineno} 行：引用了未在本文件里定义过的名字 {node.id!r}"
            )
        return namespace[node.id]

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY_OPS):
            raise _reject(node, "这种一元运算")
        operand = _eval_value(node.operand, namespace)
        if not isinstance(operand, (int, float)) or isinstance(operand, bool):
            raise _reject(node, "对非数字取正负")
        return -operand if isinstance(node.op, ast.USub) else operand

    if isinstance(node, ast.Dict):
        result = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:  # {**other}
                raise _reject(node, "字典展开")
            key = _eval_value(key_node, namespace)
            if not isinstance(key, str):
                raise DataModuleError(
                    f"第 {getattr(key_node, 'lineno', '?')} 行：字典的键必须是字符串，"
                    f"收到 {key!r}"
                )
            result[key] = _eval_value(value_node, namespace)
        return result

    # List / Tuple / Set
    for element in node.elts:
        if isinstance(element, ast.Starred):
            raise _reject(element, "星号展开")
    values = [_eval_value(element, namespace) for element in node.elts]
    if isinstance(node, ast.List):
        return values
    if isinstance(node, ast.Set):
        return set(values)
    return tuple(values)


def _is_docstring(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def evaluate_data_source(source: str, filename: str = "<data>") -> Dict:
    """把一段纯数据源码求值成命名空间。不执行任何代码。"""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        raise DataModuleError(f"{filename} 语法错误：{e}") from e

    namespace: Dict = {}
    for node in tree.body:
        if _is_docstring(node):
            continue
        if not isinstance(node, ast.Assign):
            raise _reject(node, "顶层出现赋值以外的语句")
        for target in node.targets:
            if not isinstance(target, ast.Name):
                raise _reject(
                    target, "赋值给名字以外的东西（下标、属性、解包都不行）"
                )
        value = _eval_value(node.value, namespace)
        for target in node.targets:
            namespace[target.id] = value
    return namespace


def require(namespace: Dict, name: str, expected_type, filename: str = "<data>"):
    """从命名空间里取一个必须存在、且类型正确的顶层变量。"""
    if name not in namespace:
        raise DataModuleError(f"{filename} 里找不到顶层变量 {name}")
    value = namespace[name]
    if not isinstance(value, expected_type):
        raise DataModuleError(
            f"{filename} 的 {name} 必须是 {expected_type.__name__}，"
            f"实际是 {type(value).__name__}"
        )
    return value
