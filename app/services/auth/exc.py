"""Exceptions related to the auth service"""
from app.utils.exc import BaseBccException


class JobAlreadyExists(BaseBccException):
    """Exception when new credentials are being saved yet they exist already"""

    pass


class AuthenticationError(BaseBccException):
    """Exception when credentials passed do not match those given"""

    pass


class AuthorizationError(BaseBccException):
    """Exception when credentials passed are not allowed to do a certain operation"""

    pass
