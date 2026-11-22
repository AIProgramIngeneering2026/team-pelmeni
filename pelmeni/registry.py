"""Реестр исполнителей операций. Не запускает Tool и не меняет Job."""

from .tool import Tool


class RegistryError(ValueError):
    """Ошибка ядра; преобразование в HTTP выполняет входной слой."""

    def __init__(self, code: str, message: str, field: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = [{"field": field, "code": code, "message": message}]


class Registry:
    """Имена регистрозависимы; повторная регистрация запрещена."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    @staticmethod
    def _validate_name(operation: str) -> None:
        if (
            not isinstance(operation, str)
            or not operation
            or operation != operation.strip()
        ):
            raise RegistryError(
                "INVALID_PARAMETERS",
                "Имя операции должно быть непустой строкой без пробелов по краям.",
                "operation",
            )

    def register(self, operation: str, tool: Tool) -> None:
        self._validate_name(operation)
        if operation in self._tools:
            raise RegistryError(
                "DUPLICATE_OPERATION", "Операция уже зарегистрирована.", "operation"
            )
        if isinstance(tool, type) or not callable(getattr(tool, "execute", None)):
            raise RegistryError(
                "INVALID_PARAMETERS", "Нужен экземпляр Tool с методом execute.", "tool"
            )
        self._tools[operation] = tool

    def get(self, operation: str) -> Tool:
        self._validate_name(operation)
        try:
            return self._tools[operation]
        except KeyError:
            raise RegistryError(
                "UNKNOWN_OPERATION", "Неизвестная операция.", "operation"
            ) from None
