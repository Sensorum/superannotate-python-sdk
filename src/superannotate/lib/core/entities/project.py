import uuid
from typing import Any
from typing import Iterable
from typing import List
from typing import Optional
from typing import Union

from pydantic import Extra
from pydantic import Field
from pydantic import StrictBool
from pydantic import StrictFloat
from pydantic import StrictInt
from pydantic import StrictStr

from lib.core.enums import ProjectType
from lib.core.enums import ProjectStatus
from lib.core.entities.base import BaseModel
from lib.core.entities.base import TimedBaseModel
from lib.core.entities.classes import AnnotationClassEntity


class AttachmentEntity(BaseModel):
    name: Optional[str] = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str

    class Config:
        extra = Extra.ignore


class WorkflowEntity(BaseModel):
    uuid: Optional[int]
    project_id: Optional[int]
    class_id: Optional[int]
    step: Optional[int]
    tool: Optional[int]
    attribute: Iterable = tuple(),

    def __copy__(self):
        return WorkflowEntity(step=self.step, tool=self.tool, attribute=self.attribute)


class SettingEntity(BaseModel):
    id: Optional[int]
    project_id: Optional[int]
    attribute: str
    value: Union[StrictStr, StrictInt, StrictFloat, StrictBool]

    class Config:
        extra = Extra.ignore

    def __copy__(self):
        return SettingEntity(attribute=self.attribute, value=self.value)


class ProjectEntity(TimedBaseModel):
    id: Optional[int]
    team_id: Optional[int]
    name: Optional[str]
    type: Optional[ProjectType]
    description: Optional[str]
    instructions_link: Optional[str]
    creator_id: Optional[str]
    entropy_status: Optional[int]
    sharing_status: Optional[int]
    status: Optional[ProjectStatus]
    folder_id: Optional[int]
    sync_status: Optional[int]
    upload_state: Optional[int]
    users: Optional[List[Any]] = []
    unverified_users: Optional[List[Any]] = []
    contributors: Optional[List[Any]] = []
    settings: Optional[List[SettingEntity]] = []
    classes: Optional[List[AnnotationClassEntity]] = []
    workflows: Optional[List[WorkflowEntity]] = []
    completedImagesCount: Optional[int]
    rootFolderCompletedImagesCount: Optional[int]

    class Config:
        extra = Extra.ignore
        use_enum_names = True

    def __copy__(self):
        return ProjectEntity(
            team_id=self.team_id,
            name=self.name,
            type=self.type,
            description=self.description,
            instructions_link=self.instructions_link
            if self.description
            else f"Copy of {self.name}.",
            status=self.status,
            folder_id=self.folder_id,
            users=self.users,
            upload_state=self.upload_state,
        )
