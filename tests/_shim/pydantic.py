"""Minimal ``pydantic`` replacement used only by the offline test shim."""
from __future__ import annotations


class Field:  # noqa: D401 - shim
    def __init__(self, default=..., description=None, **_kwargs):
        self.default = default
        self.description = description


class BaseModelMeta(type):
    def __new__(mcs, name, bases, namespace):
        fields: dict[str, tuple] = {}
        for base in bases:
            fields.update(getattr(base, "_fields", {}))
        for attr, annotation in namespace.get("__annotations__", {}).items():
            default = namespace.get(attr, ...)
            if isinstance(default, Field):
                default = default.default
            fields[attr] = (annotation, default)

        def __init__(self, **kwargs):
            for key in kwargs:
                if key not in fields:
                    raise TypeError(f"unexpected field {key!r}")
            for key, (_annotation, default) in fields.items():
                if key in kwargs:
                    setattr(self, key, kwargs[key])
                elif default is not ...:
                    setattr(self, key, default)
                else:
                    raise TypeError(f"missing field {key!r}")

        def model_dict(self):
            return {key: _plain(getattr(self, key)) for key in fields}

        # ``list[Model]`` endpoint results are returned as plain lists, not
        # model instances, so mirror the method on list too.
        namespace["_fields"] = fields
        namespace["__init__"] = __init__
        namespace["dict"] = model_dict
        return super().__new__(mcs, name, bases, namespace)


def _plain(value):
    if isinstance(value, BaseModel):
        return value.dict()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class BaseModel(metaclass=BaseModelMeta):
    pass
