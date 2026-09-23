from typing import Literal

from pydantic import BaseModel

from sag_api.upgrades.contracts import StorageChoice


class StorageChoiceRequest(BaseModel):
    choice: Literal[StorageChoice.FRESH]
