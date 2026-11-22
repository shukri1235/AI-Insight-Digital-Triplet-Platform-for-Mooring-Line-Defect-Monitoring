class Part:
    def __init__(self, data=None, mime_type=None):
        self.data, self.mime_type = data, mime_type

    @classmethod
    def from_bytes(cls, data, mime_type):
        return cls(data, mime_type)


class GenerateContentConfig:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class HttpRetryOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class HttpOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ThinkingConfig:
    def __init__(self, thinking_level=None, thinking_budget=None):
        if thinking_level not in (None, "LOW", "HIGH"):
            raise ValueError(f"bad thinking_level {thinking_level}")
        self.thinking_level, self.thinking_budget = thinking_level, thinking_budget
