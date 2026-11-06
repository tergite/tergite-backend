# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The values of the expressions of an OpenQASM 3 program: its numbers and its durations"""

import cmath
import math
import operator
from typing import Callable, Dict, Mapping

from openpulse import ast

from utils.exc import UncomputableExpressionError, UnsupportedCircuitError

SECONDS_PER_UNIT = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}
# OpenQASM's constants, by their ASCII and their Unicode names
CONSTANTS = {
    "pi": math.pi,
    "π": math.pi,
    "tau": math.tau,
    "τ": math.tau,
    "euler": math.e,
    "ℇ": math.e,
}
FUNCTIONS: Dict[str, Callable[[complex], complex]] = {
    "sin": cmath.sin,
    "cos": cmath.cos,
    "tan": cmath.tan,
    "arcsin": cmath.asin,
    "arccos": cmath.acos,
    "arctan": cmath.atan,
    "exp": cmath.exp,
    "log": cmath.log,
    "sqrt": cmath.sqrt,
}
BINARY_OPERATIONS: Dict[str, Callable[[complex, complex], complex]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "**": operator.pow,
}


def get_number(expression: ast.Expression, parameters: Mapping[str, complex]) -> float:
    """Gets the real number the expression comes to

    Args:
        expression: the expression
        parameters: the values of the identifiers it may name but OpenQASM's constants

    Returns:
        the number

    Raises:
        UnsupportedCircuitError: {expression} is not a number
        UncomputableExpressionError: an expression cannot be computed: {reason}
    """
    value = get_complex(expression, parameters)
    if not cmath.isfinite(value):
        raise UncomputableExpressionError("overflow")
    if abs(value.imag) > 1e-12:
        raise UnsupportedCircuitError(f"{value} is not a real number")
    return value.real


def get_complex(
    expression: ast.Expression, parameters: Mapping[str, complex]
) -> complex:
    """Gets the complex number the expression comes to, as an amplitude may be

    Args:
        expression: the expression
        parameters: the values of the identifiers it may name but OpenQASM's constants

    Returns:
        the number

    Raises:
        UnsupportedCircuitError: {expression} is not a number
        UncomputableExpressionError: an expression cannot be computed: {reason}
    """
    if isinstance(expression, (ast.IntegerLiteral, ast.FloatLiteral)):
        return complex(expression.value)
    if isinstance(expression, ast.ImaginaryLiteral):
        return complex(0, expression.value)
    if isinstance(expression, ast.Identifier):
        if expression.name in parameters:
            return complex(parameters[expression.name])
        if expression.name in CONSTANTS:
            return complex(CONSTANTS[expression.name])
    elif isinstance(expression, ast.UnaryExpression):
        if expression.op.name == "-":
            return -get_complex(expression.expression, parameters)
    elif isinstance(expression, ast.BinaryExpression):
        operation = BINARY_OPERATIONS.get(expression.op.name)
        if operation is not None:
            return _compute(
                operation,
                get_complex(expression.lhs, parameters),
                get_complex(expression.rhs, parameters),
            )
    elif isinstance(expression, ast.FunctionCall):
        function = FUNCTIONS.get(expression.name.name)
        if function is not None and len(expression.arguments) == 1:
            return _compute(function, get_complex(expression.arguments[0], parameters))
    raise UnsupportedCircuitError(f"{type(expression).__name__} is not a number")


def get_seconds(duration: ast.Expression, dt: float) -> float:
    """Gets the seconds of a duration, counted in dt or in a unit of time

    Args:
        duration: the duration, as written
        dt: the device's sample time in seconds

    Returns:
        the duration in seconds

    Raises:
        UnsupportedCircuitError: unit {unit} is not supported
        UnsupportedCircuitError: a duration of other than a number and a unit is not
            supported
    """
    if not isinstance(duration, ast.DurationLiteral):
        raise UnsupportedCircuitError(
            "a duration of other than a number and a unit is not supported"
        )
    unit = duration.unit.name
    if unit == "dt":
        return float(duration.value) * dt
    if unit not in SECONDS_PER_UNIT:
        raise UnsupportedCircuitError(f"unit {unit} is not supported")
    return float(duration.value) * SECONDS_PER_UNIT[unit]


def _compute(operation: Callable[..., complex], *operands: complex) -> complex:
    """Applies the operation to the operands

    Raises:
        UncomputableExpressionError: an expression cannot be computed: division by zero
        UncomputableExpressionError: an expression cannot be computed: overflow
        UncomputableExpressionError: an expression cannot be computed: math domain error
    """
    try:
        value = operation(*operands)
    except ZeroDivisionError as exp:
        raise UncomputableExpressionError("division by zero") from exp
    except OverflowError as exp:
        raise UncomputableExpressionError("overflow") from exp
    except ValueError as exp:
        raise UncomputableExpressionError(f"{exp}") from exp
    if not cmath.isfinite(value):
        raise UncomputableExpressionError("overflow")
    return value
