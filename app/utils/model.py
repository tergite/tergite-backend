# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utilities for models"""
import sys
from typing import (
    Any,
    Callable,
    Dict,
    Literal,
    Optional,
    Sequence,
    Set,
    TypeVar,
    Union,
    get_args,
)

from pydantic import BaseModel, ConfigDict, create_model

T = TypeVar("T", bound=BaseModel)
IncEx = Union[Set[str], Set[int], Dict[int, Any], Dict[str, Any], None]


class PartialMeta(BaseModel):
    """The base model for partial schemas that have all fields as optional"""

    model_config = ConfigDict(extra="ignore")

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        context: Any | None = None,
        by_alias: bool = False,
        exclude_unset: bool = True,
        exclude_defaults: bool = True,
        exclude_none: bool = True,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        return super().model_dump(
            mode=mode,
            include=include,
            exclude=exclude,
            context=context,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            round_trip=round_trip,
            warnings=warnings,
            fallback=fallback,
            serialize_as_any=serialize_as_any,
        )


def create_partial_schema(
    name: str, original: type[T], exclude: Sequence[str] = (), default: Any = None
) -> type[T]:
    """Creates a schema that has all its fields as optional based on the original

    Args:
        name: the name of the new model
        original: the original model whose fields are to be passed in as query params
        exclude: the fields of the original to exclude
        default: the default value of the fields

    Returns:
        the model to be used in the router
    """
    # make all fields optional so that the query parameters are optional
    fields = {
        name: (_as_optional(field.annotation), default)
        for name, field in original.model_fields.items()
        if name not in exclude
    }

    return create_model(
        name,
        # module of the calling function
        __module__=sys._getframe(1).f_globals["__name__"],
        __doc__=f"{PartialMeta.__doc__}\n\nOriginal:\n{original.__doc__}",
        __base__=(PartialMeta, original),
        **fields,
    )


def _as_optional(type__) -> type:
    """Converts the type into an optional type if it was not one already

    Args:
        type__: the type to convert

    Returns:
        The type as an optional type
    """
    if type(None) in get_args(type__):
        return type__
    return Optional[type__]
