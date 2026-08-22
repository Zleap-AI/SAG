from pydantic import BaseModel

from sag_api.upgrades.contracts import StorageChoice


class StorageChoiceRequest(BaseModel):
    choice: StorageChoice
