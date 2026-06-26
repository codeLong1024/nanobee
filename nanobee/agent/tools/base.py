"""Agent 工具基类定义。

核心逻辑完全保留：Schema 抽象基类、Tool 抽象基类、参数校验、类型转换。
"""

from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

if typing.TYPE_CHECKING:
    from pydantic import BaseModel

_ToolT = TypeVar("_ToolT", bound="Tool")

# JSON Schema 类型映射
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class Schema(ABC):
    """JSON Schema 片段的抽象基类。

    具体类型定义在 nanobee.agent.tools.schema 模块中。
    所有实现 to_json_schema 和 validate_value 方法。
    """

    @staticmethod
    def resolve_json_schema_type(t: Any) -> str | None:
        """从 JSON Schema type 中解析非空类型名（如 ['string','null'] -> 'string'）。"""
        if isinstance(t, list):
            return next((x for x in t if x != "null"), None)
        return t  # type: ignore[return-value]

    @staticmethod
    def subpath(path: str, key: str) -> str:
        return f"{path}.{key}" if path else key

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        """根据 JSON Schema 片段校验值，返回错误消息列表（空表示合法）。"""
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get("nullable", False)
        t = Schema.resolve_json_schema_type(raw_type)
        label = path or "parameter"

        if nullable and val is None:
            return []
        if t is None:
            return [f"{label} has unknown type: {raw_type!r}"]
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (
            not isinstance(val, _JSON_TYPE_MAP["number"]) or isinstance(val, bool)
        ):
            return [f"{label} should be number"]
        if t not in _JSON_TYPE_MAP:
            return [f"{label} has unsupported type: {t!r}"]
        if t not in ("integer", "number") and not isinstance(val, _JSON_TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors: list[str] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {Schema.subpath(path, k)}")
            for k, v in val.items():
                if k in props:
                    errors.extend(Schema.validate_json_schema_value(v, props[k], Schema.subpath(path, k)))
        if t == "array":
            if "minItems" in schema and len(val) < schema["minItems"]:
                errors.append(f"{label} must have at least {schema['minItems']} items")
            if "maxItems" in schema and len(val) > schema["maxItems"]:
                errors.append(f"{label} must be at most {schema['maxItems']} items")
            if "items" in schema:
                prefix = f"{path}[{{}}]" if path else "[{}]"
                for i, item in enumerate(val):
                    errors.extend(
                        Schema.validate_json_schema_value(item, schema["items"], prefix.format(i))
                    )
        return errors

    @staticmethod
    def fragment(value: Any) -> dict[str, Any]:
        """将 Schema 实例或 JSON Schema dict 标准化为片段 dict。"""
        to_js = getattr(value, "to_json_schema", None)
        if callable(to_js):
            return to_js()
        if isinstance(value, dict):
            return value
        raise TypeError(f"Expected schema object or dict, got {type(value).__name__}")

    @abstractmethod
    def to_json_schema(self) -> dict[str, Any]:
        """返回 JSON Schema 兼容的片段 dict。"""
        ...

    def validate_value(self, value: Any, path: str = "") -> list[str]:
        """校验单个值，返回错误消息列表（空表示通过）。子类可重写以添加额外规则。"""
        return Schema.validate_json_schema_value(value, self.to_json_schema(), path)


class Tool(ABC):
    """Agent 能力抽象基类：文件读写、命令执行等。"""

    _TYPE_MAP = _JSON_TYPE_MAP
    _BOOL_TRUE = frozenset(("true", "1", "yes"))
    _BOOL_FALSE = frozenset(("false", "0", "no"))

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """从 JSON Schema 联合类型（如 ['string','null']）中提取首个非空类型。"""
        return Schema.resolve_json_schema_type(t)

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于 function call 调用。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述。"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        ...

    @property
    def read_only(self) -> bool:
        """工具是否无副作用且可安全并行化。"""
        return False

    @property
    def concurrency_safe(self) -> bool:
        """工具是否可与其他并发安全工具并行执行。"""
        return self.read_only and not self.exclusive

    @property
    def exclusive(self) -> bool:
        """工具是否应独占执行（即使并发已启用）。"""
        return False

    # --- 插件元数据 ---

    config_key: str = ""
    _plugin_discoverable: bool = True
    _scopes: set[str] = {"core"}

    @classmethod
    def config_cls(cls) -> type[BaseModel] | None:
        return None

    @classmethod
    def enabled(cls) -> bool:
        """工具是否启用。Nanobee 简化版，移除 ToolContext 依赖。"""
        return True

    @classmethod
    def create(cls) -> Tool:
        """工厂方法创建工具实例。Nanobee 简化版，移除 ToolContext 依赖。"""
        return cls()

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """执行工具，返回字符串或内容块列表。"""
        ...

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return obj
        props = schema.get("properties", {})
        return {k: self._cast_value(v, props[k]) if k in props else v for k, v in obj.items()}

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """在校验前进行安全的 Schema 驱动类型转换。"""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params
        return self._cast_object(params, schema)

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        t = self._resolve_type(schema.get("type"))

        if t == "boolean" and isinstance(val, bool):
            return val
        if t == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if t in self._TYPE_MAP and t not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[t]
            if isinstance(val, expected):
                return val

        if isinstance(val, str) and t in ("integer", "number"):
            try:
                return int(val) if t == "integer" else float(val)
            except ValueError:
                return val

        if t == "string":
            return val if val is None else str(val)

        if t == "boolean" and isinstance(val, str):
            low = val.lower()
            if low in self._BOOL_TRUE:
                return True
            if low in self._BOOL_FALSE:
                return False
            return val

        if t == "array" and isinstance(val, list):
            items = schema.get("items")
            return [self._cast_value(x, items) for x in val] if items else val

        if t == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """根据 JSON Schema 校验参数，空列表表示合法。"""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return Schema.validate_json_schema_value(params, {**schema, "type": "object"}, "")

    def to_schema(self) -> dict[str, Any]:
        """转换为 OpenAI function schema 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_parameters(schema: dict[str, Any]) -> Callable[[type[_ToolT]], type[_ToolT]]:
    """类装饰器：附加 JSON Schema 并注入具体的 parameters 属性。

    用在 Tool 子类上替代手动编写 @property def parameters。
    Schema 会被深拷贝存储，每次访问返回新副本。

    Example::

        @tool_parameters({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        })
        class ReadFileTool(Tool):
            ...
    """

    def decorator(cls: type[_ToolT]) -> type[_ToolT]:
        frozen = deepcopy(schema)

        @property
        def parameters(self: Any) -> dict[str, Any]:
            return deepcopy(frozen)

        cls.parameters = parameters  # type: ignore[assignment]

        # 将 ``parameters`` 从抽象方法集合中移除，使被装饰的类可以实例化，
        # 而无需子类手动覆写 parameters 属性。
        abstract = getattr(cls, "__abstractmethods__", None)
        if abstract is not None and "parameters" in abstract:
            cls.__abstractmethods__ = frozenset(abstract - {"parameters"})  # type: ignore[misc]

        return cls

    return decorator
