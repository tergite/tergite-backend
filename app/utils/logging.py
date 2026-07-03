import logging

import settings

# error logger
err_logger = logging.getLogger("uvicorn.error")
# work around for testing to allow errors to be seen in terminal
if settings.APP_SETTINGS == "test":
    err_logger.error = print
    err_logger.info = print
    err_logger.warning = print
    err_logger.debug = print
