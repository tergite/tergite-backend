import uvicorn

from rest_api import app

import settings


class BCCApplication:

    def __init__(self,
                 host: str = settings.BCC_MACHINE_ROOT_URL,
                 port: int = settings.BCC_PORT):
        self.host: str = host
        self.port: int = port

    def run(self):
        uvicorn.run(app, host=self.host, port=self.port)