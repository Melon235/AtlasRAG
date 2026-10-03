"""Narrow PostgreSQL exception translation at infrastructure boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from typing import Concatenate, ParamSpec, Protocol, TypeVar

from psycopg import (
    DataError,
    Error,
    IntegrityError,
    NotSupportedError,
    ProgrammingError,
    errors,
)
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, TypeAdapter, ValidationError

from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
)
from atlasrag.repositories.postgres.records import CanonicalId

_RecordT = TypeVar("_RecordT", bound=BaseModel)
_FailureAwareT = TypeVar("_FailureAwareT", bound="_FailureAware")
_Params = ParamSpec("_Params")
_ResultT = TypeVar("_ResultT")
_CANONICAL_ID = TypeAdapter(CanonicalId)
_TIMEOUT_ERRORS = (
    PoolTimeout,
    TimeoutError,
    errors.CancellationTimeout,
    errors.ConnectionTimeout,
    errors.IdleInTransactionSessionTimeout,
    errors.IdleSessionTimeout,
    errors.QueryCanceled,
    errors.TransactionTimeout,
)


class _FailureAware(Protocol):
    def _repository_failed(self) -> None: ...


@contextmanager
def postgres_error_boundary() -> Iterator[None]:
    """Translate only known driver, pool, and OS dependency failures."""

    try:
        yield
    except _TIMEOUT_ERRORS:
        raise DependencyTimeoutError from None
    except (IntegrityError, DataError):
        raise InvariantViolationError from None
    except (ProgrammingError, NotSupportedError):
        raise ConfigurationError from None
    except (Error, OSError):
        raise DependencyUnavailableError from None


def marks_uow_failed(
    method: Callable[Concatenate[_FailureAwareT, _Params], Awaitable[_ResultT]],
) -> Callable[Concatenate[_FailureAwareT, _Params], Awaitable[_ResultT]]:
    """Poison an owning UoW when a repository operation fails."""

    @wraps(method)
    async def wrapped(
        self: _FailureAwareT,
        /,
        *args: _Params.args,
        **kwargs: _Params.kwargs,
    ) -> _ResultT:
        try:
            return await method(self, *args, **kwargs)
        except BaseException:
            self._repository_failed()
            raise

    return wrapped


def validate_canonical_row(model: type[_RecordT], row: object) -> _RecordT:
    """Validate a database row without leaking Pydantic or row details."""

    try:
        return model.model_validate(row)
    except ValidationError:
        raise CanonicalDataError from None


def require_canonical_id(value: object) -> str:
    """Validate a caller-supplied storage identifier as a local invariant."""

    try:
        return _CANONICAL_ID.validate_python(value)
    except ValidationError:
        raise InvariantViolationError from None


def validate_count_row(row: object) -> int:
    """Read a non-negative aggregate count without trusting driver payloads."""

    if not isinstance(row, Mapping):
        raise CanonicalDataError
    count = row.get("record_count")
    if type(count) is not int or count < 0:
        raise CanonicalDataError
    return count


def validate_identity_rows(
    rows: Sequence[object], identity_column: str
) -> tuple[str, ...]:
    """Validate deterministic exact-set identity query results."""

    identities: list[str] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping) or identity_column not in row:
                raise CanonicalDataError
            identities.append(_CANONICAL_ID.validate_python(row[identity_column]))
    except ValidationError:
        raise CanonicalDataError from None
    return tuple(identities)


def validate_rowcount(rowcount: object) -> int:
    """Require a concrete non-negative affected-row count."""

    if type(rowcount) is not int or rowcount < 0:
        raise CanonicalDataError
    return rowcount
