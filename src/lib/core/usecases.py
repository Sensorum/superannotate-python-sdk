import copy
import io
import json
import os.path
import time
import uuid
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Iterable
from typing import List
from typing import Optional

import boto3
import requests
import src.lib.core as constances
from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient
from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage as google_storage
from src.lib.core.conditions import Condition
from src.lib.core.conditions import CONDITION_EQ as EQ
from src.lib.core.entities import AnnotationClassEntity
from src.lib.core.entities import FolderEntity
from src.lib.core.entities import ImageEntity
from src.lib.core.entities import ImageInfoEntity
from src.lib.core.entities import ProjectEntity
from src.lib.core.entities import ProjectSettingEntity
from src.lib.core.entities import S3FileEntity
from src.lib.core.entities import TeamEntity
from src.lib.core.entities import WorkflowEntity
from src.lib.core.enums import ProjectType
from src.lib.core.exceptions import AppException
from src.lib.core.exceptions import AppValidationException
from src.lib.core.plugin import ImagePlugin
from src.lib.core.plugin import VideoPlugin
from src.lib.core.repositories import BaseManageableRepository
from src.lib.core.repositories import BaseProjectRelatedManageableRepository
from src.lib.core.repositories import BaseReadOnlyRepository
from src.lib.core.response import Response
from src.lib.core.serviceproviders import SuerannotateServiceProvider


class BaseUseCase(ABC):
    def __init__(self, response: Response):
        self._response = response
        self._errors = []

    @abstractmethod
    def execute(self):
        raise NotImplementedError

    def _validate(self):
        for name in dir(self):
            try:
                if name.startswith("validate_"):
                    method = getattr(self, name)
                    method()
            except AppValidationException as e:
                self._errors.append(e)

    def is_valid(self):
        self._validate()
        return not self._errors


class GetProjectsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        condition: Condition,
        team_id: int,
        projects: BaseManageableRepository,
    ):
        super().__init__(response)
        self._condition = condition
        self._projects = projects
        self._team_id = team_id

    def execute(self):
        if self.is_valid():
            condition = self._condition & Condition("team_id", self._team_id, EQ)
            self._response.data = self._projects.get_all(condition)
        self._response.errors = self._errors


class CreateProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        projects: BaseManageableRepository,
        backend_service_provider: SuerannotateServiceProvider,
        settings: List[ProjectSettingEntity] = None,
        workflows: List[WorkflowEntity] = None,
        annotation_classes: List[AnnotationClassEntity] = None,
        contributors: Iterable[dict] = None,
    ):

        super().__init__(response)
        self._project = project
        self._projects = projects
        self._settings = settings
        self._workflows = workflows
        self._annotation_classes = annotation_classes
        self._contributors = contributors
        self._backend_service = backend_service_provider

    def execute(self):
        if self.is_valid():
            # todo add status in the constanses
            self._project.status = 0
            entity = self._projects.insert(self._project)
            self._response.data = entity
            if self._settings:
                settings_repo = BaseProjectRelatedManageableRepository(
                    self._backend_service, entity
                )
                for setting in self._settings:
                    settings_repo.insert(setting)
                self._response.data.settings = self._settings
            annotation_classes_mapping = {}
            if self._annotation_classes:
                annotation_repo = BaseProjectRelatedManageableRepository(
                    self._backend_service, entity
                )
                for annotation_class in self._annotation_classes:
                    annotation_classes_mapping[
                        annotation_class.uuid
                    ] = annotation_repo.insert(annotation_class)
                self._response.data.annotation_classes = self._annotation_classes
            if self._workflows:
                workflow_repo = BaseProjectRelatedManageableRepository(
                    self._backend_service, entity
                )
                for workflow in self._workflows:
                    workflow.project_id = entity.uuid
                    workflow.class_id = annotation_classes_mapping.get(
                        workflow.class_id
                    )
                    workflow_repo.insert(workflow)
                self._response.data.workflows = self._workflows

            if self._contributors:
                for contributor in self.contributors:
                    self._backend_service.share_project(
                        entity.uuid,
                        entity.team_id,
                        contributor.get("id"),
                        contributor.get("role"),
                    )
                self._response.data.contributors = self._contributors
        else:
            self._response.errors = self._errors

    def validate_project_name_uniqueness(self):
        condition = Condition("name", self._project.name, EQ) & Condition(
            "team_id", self._project.team_id, EQ
        )
        if self._projects.get_all(condition):
            raise AppValidationException(
                f"Project name {self._project.name} is not unique. "
                f"To use SDK please make project names unique."
            )


class DeleteProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        projects: BaseManageableRepository,
    ):

        super().__init__(response)
        self._project = project
        self._projects = projects

    def execute(self):
        if self.is_valid():
            self._projects.delete(self._project)
        else:
            self._response.errors = self._errors


class UpdateProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        projects: BaseManageableRepository,
    ):

        super().__init__(response)
        self._project = project
        self._projects = projects

    def execute(self):
        if self.is_valid():
            self._projects.update(self._project)
        else:
            self._response.errors = self._errors


class CloneProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        project_to_create: ProjectEntity,
        projects: BaseManageableRepository,
        settings: BaseManageableRepository,
        workflows: BaseManageableRepository,
        annotation_classes: BaseManageableRepository,
        backend_service_provider: SuerannotateServiceProvider,
        include_annotation_classes: bool = True,
        include_settings: bool = True,
        include_workflow: bool = True,
        include_contributors: bool = False,
    ):
        super().__init__(response)
        self._project = project
        self._project_to_create = project_to_create
        self._projects = projects
        self._settings = settings
        self._workflows = workflows
        self._annotation_classes = annotation_classes
        self._backend_service = backend_service_provider
        self._include_annotation_classes = include_annotation_classes
        self._include_settings = include_settings
        self._include_workflow = include_workflow
        self._include_contributors = include_contributors

    def execute(self):
        project = self._projects.insert(self._project_to_create)
        self._response.data = project
        annotation_classes_mapping = {}
        if self._include_annotation_classes:
            annotation_classes = self._annotation_classes.get_all()
            for annotation_class in annotation_classes:
                annotation_class_copy = copy.copy(annotation_class)
                annotation_class_copy.project_id = project.uuid
                annotation_classes_mapping[
                    annotation_class.uuid
                ] = self._annotation_classes.insert(annotation_class_copy).uuid

        if self._include_contributors:
            for user in self._project.users:
                self._backend_service.share_project(
                    project.uuid, project.team_id, user.get("id"), user.get("role")
                )

        if self._include_settings:
            for setting in self._settings.get_all():
                setting_copy = copy.copy(setting)
                setting_copy.project_id = project.uuid
                self._settings.insert(setting)

        if self._include_workflow:
            for workflow in self._workflows.get_all():
                workflow_copy = copy.copy(workflow)
                workflow_copy.project_id = project.uuid
                workflow_copy.class_id = annotation_classes_mapping[workflow.class_id]
                self._workflows.insert(workflow_copy)


class AttachImagesUseCase(BaseUseCase):
    """
    Attach urls
    """

    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        project_settings: BaseReadOnlyRepository,
        backend_service_provider: SuerannotateServiceProvider,
        images: List[ImageEntity],
        annotation_status: Optional[str] = None,
        image_quality: Optional[str] = None,
    ):
        super().__init__(response)
        self._project = project
        self._project_settings = project_settings
        self._backend = backend_service_provider
        self._images = images
        self._annotation_status = annotation_status
        self._image_quality = image_quality

    @property
    def image_quality(self):
        if not self._image_quality:
            for setting in self._project_settings.get_all():
                if setting.attribute == "ImageQuality":
                    if setting.value == 60:
                        return "compressed"
                    elif setting.value == 100:
                        return "original"
                    raise AppException("NA ImageQuality value")
        return self._image_quality

    @property
    def upload_state_code(self) -> int:
        return constances.UploadState.BASIC.value

    @property
    def annotation_status_code(self):
        if not self._annotation_status:
            return constances.AnnotationStatus.NOT_STARTED.value
        return constances.AnnotationStatus.get_value(self._annotation_status)

    def execute(self):
        images = []
        meta = {}
        for image in self._images:
            images.append({"name": image.name, "path": image.path})
            meta[image.name] = {"width": image.meta.width, "height": image.meta.height}

        self._backend.attach_files(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            files=images,
            annotation_status_code=self.annotation_status_code,
            upload_state_code=self.upload_state_code,
            meta=meta,
        )

    def validate_upload_state(self):
        if self._project.upload_state == constances.UploadState.EXTERNAL.value:
            raise AppValidationException("Invalid upload state.")


class GetImagesUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folder: FolderEntity,
        images: BaseReadOnlyRepository,
        annotation_status: str = None,
        image_name_prefix: str = None,
    ):
        super().__init__(response)
        self._project = project
        self._folder = folder
        self._images = images
        self._annotation_status = annotation_status
        self._image_name_prefix = image_name_prefix

    def execute(self):
        condition = (
            Condition("team_id", self._project.team_id, EQ)
            & Condition("project_id", self._project.uuid, EQ)
            & Condition("folder_id", self._folder.uuid, EQ)
        )
        if self._image_name_prefix:
            condition = condition & Condition("name", self._image_name_prefix, EQ)
        if self._annotation_status:
            condition = condition & Condition(
                "annotation_status",
                constances.AnnotationStatus[self._annotation_status.upper()].value,
                EQ,
            )

        self._response.data = self._images.get_all(condition)


class GetImageUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folder: FolderEntity,
        image_name: str,
        images: BaseReadOnlyRepository,
    ):
        super().__init__(response)
        self._project = project
        self._folder = folder
        self._images = images
        self._image_name = image_name

    def execute(self):
        condition = (
            Condition("team_id", self._project.team_id, EQ)
            & Condition("project_id", self._project.uuid, EQ)
            & Condition("folder_id", self._folder.uuid, EQ)
            & Condition("name", self._image_name, EQ)
        )
        self._response.data = self._images.get_all(condition)[0]


class UploadImageS3UseCas(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        project_settings: BaseReadOnlyRepository,
        image_path: str,
        image: io.BytesIO,
        s3_repo: BaseManageableRepository,
        upload_path: str,
    ):
        super().__init__(response)
        self._project = project
        self._project_settings = project_settings
        self._image_path = image_path
        self._image = image
        self._s3_repo = s3_repo
        self._upload_path = upload_path

    @property
    def max_resolution(self) -> int:
        if self._project.project_type == ProjectType.VECTOR.value:
            return constances.MAX_VECTOR_RESOLUTION
        elif self._project.project_type == ProjectType.PIXEL.value:
            return constances.MAX_PIXEL_RESOLUTION

    def execute(self):
        image_name = Path(self._image_path).name
        image_processor = ImagePlugin(self._image, self.max_resolution)
        origin_width, origin_height = image_processor.get_size()
        thumb_image, _, _ = image_processor.generate_thumb()
        huge_image, huge_width, huge_height = image_processor.generate_huge()
        low_resolution_image, _, _ = image_processor.generate_low_resolution()

        image_key = (
            self._upload_path + str(uuid.uuid4()) + Path(self._image_path).suffix
        )

        file_entity = S3FileEntity(uuid=image_key, data=self._image)

        thumb_image_name = image_key + "___thumb.jpg"
        thumb_image_entity = S3FileEntity(uuid=thumb_image_name, data=thumb_image)
        self._s3_repo.insert(thumb_image_entity)

        low_resolution_image_name = image_key + "___lores.jpg"
        low_resolution_file_entity = S3FileEntity(
            uuid=low_resolution_image_name, data=low_resolution_image
        )
        self._s3_repo.insert(low_resolution_file_entity)

        huge_image_name = image_key + "___huge.jpg"
        huge_file_entity = S3FileEntity(
            uuid=huge_image_name,
            data=huge_image,
            metadata={"height": huge_width, "weight": huge_height},
        )
        self._s3_repo.insert(huge_file_entity)
        file_entity.data.seek(0)
        self._s3_repo.insert(file_entity)
        self._response.data = ImageEntity(
            name=image_name,
            path=image_key,
            meta=ImageInfoEntity(width=origin_width, height=origin_height),
        )


class CreateFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        folder: FolderEntity,
        folders: BaseManageableRepository,
    ):
        super().__init__(response)
        self._folder = folder
        self._folders = folders

    def execute(self):
        self._response.data = self._folders.insert(self._folder)

    def validate_folder_name(self):
        if (
            len(
                set(self._folder.name).intersection(
                    constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                )
            )
            > 0
        ):
            raise AppValidationException("New folder name has special characters.")


class AttachFileUrlsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        attachments: List[ImageEntity],
        limit: int,
        backend_service_provider: SuerannotateServiceProvider,
        annotation_status: int = constances.AnnotationStatus.NOT_STARTED.value,
    ):
        super().__init__(response)
        self._attachments = attachments
        self._project = project
        self._limit = limit
        self._backend_service = backend_service_provider
        self._annotation_status_code = annotation_status

    @property
    def annotation_status(self):
        if self._annotation_status_code:
            return self._annotation_status_code
        return constances.AnnotationStatus.NOT_STARTED.value

    def execute(self):
        files = [
            {"name": entity.name, "path": entity.name} for entity in self._attachments
        ]
        meta = {
            entity.name: {"height": entity.meta.height, "width": entity.meta.width}
            for entity in self._attachments
        }
        self._backend_service.attach_files(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            files=files[: self._limit],
            annotation_status_code=self.annotation_status,
            upload_state_code=constances.UploadState.EXTERNAL.value,
            meta=meta,
        )


class PrepareExportUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folder_names: List[str],
        backend_service_provider: SuerannotateServiceProvider,
        include_fuse: bool,
        only_pinned: bool,
        annotation_statuses: List[str] = None,
    ):
        super().__init__(response),
        self._project = project
        self._folder_names = folder_names
        self._backend_service = backend_service_provider
        self._annotation_statuses = annotation_statuses
        self._include_fuse = include_fuse
        self._only_pinned = only_pinned

    def execute(self):
        if self._project.upload_state == constances.UploadState.EXTERNAL.value:
            self._include_fuse = False

        if not self._annotation_statuses:
            self._annotation_statuses = (
                constances.AnnotationStatus.IN_PROGRESS.name,
                constances.AnnotationStatus.COMPLETED.name,
                constances.AnnotationStatus.QUALITY_CHECK.name,
                constances.AnnotationStatus.RETURNED.name,
            )

        res = self._backend_service.prepare_export(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folders=self._folder_names,
            annotation_statuses=self._annotation_statuses,
            include_fuse=self._include_fuse,
            only_pinned=self._only_pinned,
        )
        self._response.data = res


class GetTeamUseCase(BaseUseCase):
    def __init__(self, response: Response, teams: BaseReadOnlyRepository, team_id: int):
        super().__init__(response)
        self._teams = teams
        self._team_id = team_id

    def execute(self):
        self._response.data = self._teams.get_one(self._team_id)


class InviteContributorUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        backend_service_provider: SuerannotateServiceProvider,
        email: str,
        team_id: int,
        is_admin: bool = False,
    ):
        super().__init__(response)
        self._backend_service = backend_service_provider
        self._email = email
        self._team_id = team_id
        self._is_admin = is_admin

    def execute(self):
        role = (
            constances.UserRole.ADMIN.value
            if self._is_admin
            else constances.UserRole.ANNOTATOR.value
        )
        self._backend_service.invite_contributor(
            team_id=self._team_id, email=self._email, user_role=role
        )


class DeleteContributorInvitationUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        backend_service_provider: SuerannotateServiceProvider,
        team: TeamEntity,
        email: str,
    ):
        super().__init__(response)
        self._backend_service = backend_service_provider
        self._email = email
        self._team = team

    def execute(self):
        for invite in self._team.pending_invitations:
            if invite["email"] == self._email:
                self._backend_service.delete_team_invitation(
                    self._team.uuid, invite["token"], self._email
                )


class SearchContributorsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        backend_service_provider: SuerannotateServiceProvider,
        team_id: int,
        condition: Condition = None,
    ):
        super().__init__(response)
        self._backend_service = backend_service_provider
        self._team_id = team_id
        self._condition = condition

    def execute(self):
        res = self._backend_service.search_team_contributors(
            self._team_id, self._condition.build_query()
        )
        self._response.data = res


class GetFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folders: BaseReadOnlyRepository,
        folder_name: str,
    ):
        super().__init__(response)
        self._project = project
        self._folders = folders
        self._folder_name = folder_name

    def execute(self):
        condition = (
            Condition("name", self._folder_name, EQ)
            & Condition("team_id", self._project.team_id, EQ)
            & Condition("project_id", self._project.uuid, EQ)
        )
        self._response.data = self._folders.get_one(condition)


class SearchFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folders: BaseReadOnlyRepository,
        condition: Condition,
    ):
        super().__init__(response)
        self._project = project
        self._folders = folders
        self._condition = condition

    def execute(self):
        self._response.data = self._folders.get_all(self._condition)


class GetProjectFoldersUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folders: BaseReadOnlyRepository,
    ):
        super().__init__(response)
        self._project = project
        self._folders = folders

    def execute(self):
        condition = Condition("team_id", self._project.team_id, EQ) & Condition(
            "project_id", self._project.uuid, EQ
        )
        self._response.data = self._folders.get_all(condition)


class DeleteFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folders: BaseManageableRepository,
        folders_to_delete: List[FolderEntity],
    ):
        super().__init__(response)
        self._project = project
        self._folders = folders
        self._folders_to_delete = folders_to_delete

    def execute(self):
        for folder in self._folders_to_delete:
            self._folders.delete(folder.uuid)


class UpdateFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        folders: BaseManageableRepository,
        folder: FolderEntity,
    ):
        super().__init__(response)
        self._folders = folders
        self._folder = folder

    def execute(self):
        self._folders.update(self._folder)
        self._response.data = self._folder


class DownloadImageUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        image: ImageEntity,
        backend_service_provider: SuerannotateServiceProvider,
        image_variant: str = "original",
    ):
        super().__init__(response)
        self._image = image
        self._backend_service = backend_service_provider
        self._image_variant = image_variant

    def execute(self):
        auth_data = self._backend_service.get_download_token(
            project_id=self._image.project_id,
            team_id=self._image.team_id,
            folder_id=self._image.folder_id,
            image_id=self._image.uuid,
            include_original=1,
        )
        download_url = auth_data[self._image_variant]["url"]
        headers = auth_data[self._image_variant]["headers"]
        response = requests.get(url=download_url, headers=headers)
        self._response.data = io.BytesIO(response.content)


class CopyImageAnnotationClasses(BaseUseCase):
    def __init__(
        self,
        response: Response,
        from_project: ProjectEntity,
        to_project: ProjectEntity,
        from_image: ImageEntity,
        to_image: ImageEntity,
        from_project_s3_repo: BaseManageableRepository,
        to_project_s3_repo: BaseManageableRepository,
        to_project_annotation_classes: BaseReadOnlyRepository,
        from_project_annotation_classes: BaseReadOnlyRepository,
        backend_service_provider: SuerannotateServiceProvider,
        from_folder: FolderEntity = None,
        to_folder: FolderEntity = None,
        annotation_type: str = "MAIN",
    ):
        super().__init__(response)
        self._from_project = from_project
        self._to_project = to_project
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._from_project_annotation_classes = from_project_annotation_classes
        self._to_project_annotation_classes = to_project_annotation_classes
        self._from_project_s3_repo = from_project_s3_repo
        self.to_project_s3_repo = to_project_s3_repo
        self._from_image = from_image
        self._to_image = to_image
        self._backend_service = backend_service_provider
        self._annotation_type = annotation_type

    @property
    def default_annotation(self):
        return {
            "annotation_json": None,
            "annotation_json_filename": None,
            "annotation_mask": None,
            "annotation_mask_filename": None,
        }

    @property
    def annotation_json_name(self):
        if self._project.project_type == constances.ProjectType.VECTOR.value:
            return f"{self._image.name}___objects.json"
        elif self._project.project_type == constances.ProjectType.PIXEL.value:
            return f"{self._image.name}___pixel.json"

    @property
    def download_auth_data(self):
        return self._backend_service.get_download_token(
            project_id=self._from_image.project_id,
            team_id=self._from_image.team_id,
            folder_id=self._from_image.folder_id,
            image_id=self._from_image.uuid,
            include_original=1,
        )

    @property
    def upload_auth_data(self):
        return self._backend_service.get_upload_token(
            project_id=self._to_image.project_id,
            team_id=self._to_image.team_id,
            folder_id=self._to_image.folder_id,
            image_id=self._to_image.uuid,
        )

    def validate_project_type(self):
        if self._from_project.project_type != self._to_project.project_type:
            raise AppValidationException("Projects are different.")

    def execute(self):
        if self._annotation_type not in self.download_auth_data["annotations"]:
            self._response.data = self.default_annotation
            return
        annotations = self.download_auth_data["annotations"][self._annotation_type][0]
        response = requests.get(
            url=annotations["annotation_json_path"]["url"],
            headers=annotations["annotation_json_path"]["headers"],
        )
        if not response.ok:
            raise AppException(f"Couldn't load annotations {response.text}")

        image_annotation_classes = response.json()
        from_project_annotation_classes = (
            self._from_project_annotation_classes.get_all()
        )
        to_project_annotation_classes = self._to_project_annotation_classes.get_all()

        annotations_classes_from_copy = {
            from_annotation.uuid: from_annotation
            for from_annotation in from_project_annotation_classes
            for to_annotation in to_project_annotation_classes
            if from_annotation.name == to_annotation.name
        }

        annotations_classes_to_copy = {
            to_annotation.name: to_annotation
            for to_annotation in to_project_annotation_classes
            for from_annotation in from_project_annotation_classes
            if from_annotation.name == to_annotation.name
        }

        for annotation_class in image_annotation_classes["instances"]:
            project_annotation = annotations_classes_from_copy[
                annotation_class["classId"]
            ]
            annotation_class["className"] = project_annotation.name
            if annotation_class.get("attributes"):
                for attribute in annotation_class["attributes"]:
                    attribute_group = None
                    if attribute.get("groupId"):
                        for group in project_annotation.attribute_groups:
                            if group["id"] == attribute["groupId"]:
                                attribute["groupName"] = group["name"]
                                attribute_group = group
                        if attribute.get("id") and attribute_group:
                            for attr in attribute_group["attributes"]:
                                if attr["id"] == attribute["id"]:
                                    attribute["name"] = attr["name"]

        for instance in image_annotation_classes["instances"]:
            if (
                "className" not in instance
                and instance["className"] not in annotations_classes_to_copy
            ):
                continue
            annotation_class = annotations_classes_to_copy[instance["className"]]
            attribute_groups_map = {
                group["name"]: group for group in annotation_class.attribute_groups
            }
            instance["classId"] = annotation_class.uuid
            for attribute in instance["attributes"]:
                if attribute.get("groupName"):
                    attribute["groupId"] = attribute_groups_map[attribute["groupName"]][
                        "id"
                    ]
                    attr_map = {
                        attr["name"]: attr
                        for attr in attribute_groups_map[attribute["groupName"]][
                            "attributes"
                        ]
                    }
                    if attribute["name"] not in attr_map:
                        del attribute["groupId"]
                        continue
                    attribute["id"] = attr_map[attribute["name"]]["id"]

        auth_data = self.upload_auth_data
        file = S3FileEntity(
            uuid=auth_data["annotation_json_path"]["filePath"],
            data=json.dumps(image_annotation_classes),
        )
        self.to_project_s3_repo.insert(file)

        if (
            self._to_project.project_type == constances.ProjectType.PIXEL.value
            and annotations.get("annotation_bluemap_path")
            and annotations["annotation_bluemap_path"]["exist"]
        ):
            response = requests.get(
                url=annotations["annotation_bluemap_path"]["url"],
                headers=annotations["annotation_bluemap_path"]["headers"],
            )
            if not response.ok:
                raise AppException(f"Couldn't load annotations {response.text}")
            self.to_project_s3_repo.insert(
                S3FileEntity(
                    auth_data["annotation_bluemap_path"]["filePath"], response.content
                )
            )


class UpdateImageUseCase(BaseUseCase):
    def __init__(
        self, response: Response, image: ImageEntity, images: BaseManageableRepository
    ):
        super().__init__(response)
        self._image = image
        self._images = images

    def execute(self):
        self._images.update(self._image)


class DownloadImageFromPublicUrlUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        image_url: str,
        image_name: str = None,
    ):
        super().__init__(response)
        self._project = project
        self._image_url = image_url
        self._image_name = image_name

    def validate_project_type(self):
        if self._project.upload_state == constances.UploadState.EXTERNAL.value:
            raise AppValidationException(
                "The function does not support projects containing images attached with URLs"
            )

    def execute(self):
        try:
            response = requests.get(url=self._image_url)
            self._response.data = io.BytesIO(response.content)
        except requests.exceptions.RequestException as e:
            self._response.errors = AppException(
                f"Couldn't download image {self._image_url}, {e}"
            )


class ImagesBulkCopyUseCase(BaseUseCase):
    """
    Copy images in bulk between folders in a project.
    Return skipped image names.
    """

    CHUNK_SIZE = 1000

    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        from_folder: FolderEntity,
        to_folder: FolderEntity,
        image_names: List[str],
        backend_service_provider: SuerannotateServiceProvider,
        include_annotations: bool,
        include_pin: bool,
    ):
        super().__init__(response)
        self._project = project
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._image_names = image_names
        self._backend_service = backend_service_provider
        self._include_annotations = include_annotations
        self._include_pin = include_pin

    def execute(self):
        images = self._backend_service.get_bulk_images(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._to_folder.uuid,
            images=self._image_names,
        )
        duplications = [image["name"] for image in images]
        images_to_copy = set(self._image_names) - set(duplications)
        skipped_images = duplications
        for i in range(0, len(images_to_copy), self.CHUNK_SIZE):
            poll_id = self._backend_service.copy_images_between_folders_transaction(
                team_id=self._project.team_id,
                project_id=self._project.uuid,
                from_folder_id=self._from_folder.uuid,
                to_folder_id=self._to_folder.uuid,
                images=self._image_names[i : i + self.CHUNK_SIZE],
                include_annotations=self._include_annotations,
                include_pin=self._include_pin,
            )
            if not poll_id:
                skipped_images.append(self._image_names[i : i + self.CHUNK_SIZE])
                continue

            await_time = len(images_to_copy) * 0.3
            timeout_start = time.time()
            while time.time() < timeout_start + await_time:
                done_count, skipped_count = self._backend_service.get_progress(
                    self._project.uuid, self._project.team_id, poll_id
                )
                if done_count + skipped_count == len(images_to_copy):
                    break
                time.sleep(4)

        self._response.data = skipped_images


class GetAnnotationClassesUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        classes: BaseManageableRepository,
        condition: Condition = None,
    ):
        super().__init__(response)
        self._classes = classes
        self._condition = condition

    def execute(self):
        self._response.data = self._classes.get_all(condition=self._condition)


class GetSettingsUseCase(BaseUseCase):
    def __init__(self, response: Response, settings: BaseManageableRepository):
        super().__init__(response)
        self._settings = settings

    def execute(self):
        self._response.data = self._settings.get_all()


class GetWorkflowsUseCase(BaseUseCase):
    def __init__(self, response: Response, workflows: BaseManageableRepository):
        super().__init__(response)
        self._workflows = workflows

    def execute(self):
        self._response.data = self._workflows.get_all()


class GetProjectMetaDataUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        include_annotation_classes: bool,
        include_settings: bool,
        include_workflow: bool,
        include_contributors: bool,
        include_complete_image_count: bool,
        annotation_classes_repo: BaseManageableRepository,
        project_settings_repo: BaseManageableRepository,
        workflow_repo: BaseManageableRepository,
        projects_repo: BaseManageableRepository,
    ):
        super().__init__(response)
        self._project = project
        self._include_annotation_classes = include_annotation_classes
        self._include_settings = include_settings
        self._include_workflow = include_workflow
        self._annotation_classes_repo = annotation_classes_repo
        self._project_settings_repo = project_settings_repo
        self._workflow_repo = workflow_repo
        self._projects_repo = projects_repo
        self._include_contributors = include_contributors
        self._include_complete_image_count = include_complete_image_count

    def execute(self):
        res = {"project": self._project}
        if self._include_annotation_classes:
            res["annotation_classes"] = self._annotation_classes_repo.get_all()
        if self._include_settings:
            res["settings"] = self._project_settings_repo.get_all()
        if self._include_workflow:
            res["workflow"] = self._workflow_repo.get_all()
        if self._include_contributors:
            res["contributors"] = self._project.users
        if self._include_complete_image_count:
            res["project"] = self._projects_repo.get_all(
                condition=(
                    Condition("completeImagesCount", "true", EQ)
                    & Condition("name", self._project.name, EQ)
                    & Condition("team_id", self._project.team_id, EQ)
                )
            )

        self._response.data = res


class UpdateSettingsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        settings: BaseManageableRepository,
        to_update: List,
        backend_service_provider: SuerannotateServiceProvider,
        project_id: int,
        team_id: int,
    ):
        super().__init__(response)
        self._settings = settings
        self._to_update = to_update
        self._backend_service_provider = backend_service_provider
        self._project_id = project_id
        self._team_id = team_id

    def execute(self):

        old_settings = self._settings.get_all()
        attr_id_mapping = {}
        for setting in old_settings:
            attr_id_mapping[setting.attribute] = setting.uuid

        new_settings_to_update = []
        for new_setting in self._to_update:
            new_settings_to_update.append(
                {
                    "id": attr_id_mapping[new_setting["attribute"]],
                    "attribute": new_setting["attribute"],
                    "value": new_setting["value"],
                }
            )

        self._response.data = self._backend_service_provider.set_project_settings(
            project_id=self._project_id,
            team_id=self._team_id,
            data=new_settings_to_update,
        )


class DeleteImageUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        images: BaseManageableRepository,
        image: ImageEntity,
        team_id: int,
        project_id: int,
    ):
        super().__init__(response)
        self._images = images
        self._image = image
        self._team_id = team_id
        self._project_id = project_id

    def execute(self):
        self._images.delete(self._image.uuid, self._team_id, self._project_id)


class GetImageMetadataUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        image_names: list,
        team_id: int,
        project_id: int,
        service: SuerannotateServiceProvider,
    ):
        super().__init__(response)
        self._image_names = image_names
        self._project_id = project_id
        self._service = service
        self._team_id = team_id

    def execute(self):
        self._response.data = self._service.get_images_bulk(
            image_names=self._image_names,
            team_id=self._team_id,
            project_id=self._project_id,
        )


class ImagesBulkMoveUseCase(BaseUseCase):
    """
    Copy images in bulk between folders in a project.
    Return skipped image names.
    """

    CHUNK_SIZE = 1000

    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        from_folder: FolderEntity,
        to_folder: FolderEntity,
        image_names: List[str],
        backend_service_provider: SuerannotateServiceProvider,
    ):
        super().__init__(response)
        self._project = project
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._image_names = image_names
        self._backend_service = backend_service_provider

    def execute(self):
        moved_images = []
        for i in range(0, len(self._image_names), self.CHUNK_SIZE):
            moved_images.append(
                self._backend_service.move_images_between_folders(
                    team_id=self._project.team_id,
                    project_id=self._project.uuid,
                    from_folder_id=self._from_folder.uuid,
                    to_folder_id=self._to_folder.uuid,
                    images=self._image_names[i : i + self.CHUNK_SIZE],  # noqa: E203
                )
            )
        self._response.data = moved_images


class SetImageAnnotationStatuses(BaseUseCase):
    CHUNK_SIZE = 500

    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        image_names: list,
        team_id: int,
        project_id: int,
        folder_id: int,
        annotation_status: int,
    ):
        super().__init__(response)
        self._service = service
        self._image_names = image_names
        self._team_id = team_id
        self._project_id = project_id
        self._folder_id = folder_id
        self._annotation_status = annotation_status

    def execute(self):
        for i in range(0, len(self._image_names), self.CHUNK_SIZE):
            self._response.data = self._service.set_images_statuses_bulk(
                image_names=self._image_names,
                team_id=self._team_id,
                project_id=self._project_id,
                folder_id=self._folder_id,
                annotation_status=self._annotation_status,
            )


class DeleteImagesUseCase(BaseUseCase):
    CHUNK_SIZE = 1000

    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folder: FolderEntity,
        backend_service_provider: SuerannotateServiceProvider,
        images: BaseReadOnlyRepository,
        image_names: List[str] = None,
    ):
        super().__init__(response)
        self._project = project
        self._folder = folder
        self._images = images
        self._backend_service = backend_service_provider
        self._image_names = image_names

    def execute(self):
        if self._image_names:
            image_ids = [
                image["id"]
                for image in self._backend_service.get_bulk_images(
                    project_id=self._project.uuid,
                    team_id=self._project.team_id,
                    folder_id=self._folder.uuid,
                    images=self._image_names,
                )
            ]
        else:
            condition = (
                Condition("team_id", self._project.team_id, EQ)
                & Condition("project_id", self._project.uuid, EQ)
                & Condition("folder_id", self._folder.uuid, EQ)
            )
            image_ids = [image.uuid for image in self._images.get_all(condition)]

        for i in range(0, len(image_ids), self.CHUNK_SIZE):
            self._backend_service.delete_images(
                project_id=self._project.uuid,
                team_id=self._project.team_id,
                image_ids=image_ids[i : i + self.CHUNK_SIZE],
            )


class AssignImagesUseCase(BaseUseCase):

    CHUNK_SIZE = 500

    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        folder_name: str,
        image_names: list,
        user: str,
    ):
        super().__init__(response)
        self._response = response
        self._project_entity = project_entity
        self._folder_name = folder_name
        self._image_names = image_names
        self._user = user
        self._service = service

    def execute(self):
        for i in range(0, len(self._image_names), self.CHUNK_SIZE):
            self._response.data = self._service.assign_images(
                team_id=self._project_entity.team_id,
                project_id=self._project_entity.uuid,
                folder_name=self._folder_name,
                user=self._user,
                image_names=self._image_names[i : i + self.CHUNK_SIZE],
            )


class UnAssignImagesUseCase(BaseUseCase):

    CHUNK_SIZE = 500

    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        folder_name: str,
        image_names: list,
    ):
        super().__init__(response)
        self._response = response
        self._project_entity = project_entity
        self._folder_name = folder_name
        self._image_names = image_names
        self._service = service

    def execute(self):
        for i in range(0, len(self._image_names), self.CHUNK_SIZE):
            self._response.data = self._service.unassign_images(
                team_id=self._project_entity.team_id,
                project_id=self._project_entity.uuid,
                folder_name=self._folder_name,
                image_names=self._image_names[i : i + self.CHUNK_SIZE],
            )


class UnAssignFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        folder_name: str,
    ):
        super().__init__(response)
        self._response = response
        self._service = service
        self._project_entity = project_entity
        self._folder_name = folder_name

    def execute(self):
        self._response.data = self._service.un_assign_folder(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            folder_name=self._folder_name,
        )


class AssignFolderUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        folder_name: str,
        users: List[str],
    ):
        super().__init__(response)
        self._response = response
        self._service = service
        self._project_entity = project_entity
        self._folder_name = folder_name
        self._users = users

    def execute(self):
        self._response.data = self._service.assign_folder(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            folder_name=self._folder_name,
            users=self._users,
        )


class ShareProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        user_id: str,
        user_role: str,
    ):
        super().__init__(response)
        self._response = response
        self._service = service
        self._project_entity = project_entity
        self._user_id = user_id
        self._user_role = user_role

    @property
    def user_role(self):
        return constances.UserRole.get_value(self._user_role)

    def execute(self):
        self._response.data = self._service.share_project(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            user_id=self._user_id,
            user_role=self.user_role,
        )


class UnShareProjectUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project_entity: ProjectEntity,
        user_id: str,
    ):
        super().__init__(response)
        self._response = response
        self._service = service
        self._project_entity = project_entity
        self._user_id = user_id

    def execute(self):
        self._response.data = self._service.un_share_project(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            user_id=self._user_id,
        )


class DownloadGoogleCloudImages(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project_name: str,
        bucket_name: str,
        folder_name: str,
        download_path: str,
    ):
        super().__init__(response)
        self._project_name = project_name
        self._bucket_name = bucket_name
        self._folder_name = folder_name
        self._download_path = download_path

    @property
    def client(self):
        return google_storage.Client(project=self._project_name)

    def execute(self):
        bucket = self.client.get_bucket(self._bucket_name)
        image_blobs = bucket.list_blobs(prefix=self._folder_name)
        downloaded_images = []
        duplicated_images = []
        failed_images = []
        path = Path(self._download_path)
        for image_blob in image_blobs:
            if image_blobs.content_type.startswith("image"):
                image_name = os.path.basename(image_blob.name)
                image_path = path / image_name
                if image_name not in downloaded_images:
                    try:
                        image_blob.download_to_filename(image_path)
                    except GoogleAPIError as e:
                        self._response.errors = (
                            f"Couldn't download image {image_name} {e}"
                        )
                        failed_images.append(image_name)
                    else:
                        downloaded_images.append(image_name)
                else:
                    duplicated_images.append(image_name)

        self._response.data = {
            "downloaded_images": downloaded_images,
            "duplicated_images": duplicated_images,
            "failed_images": failed_images,
        }


class DownloadAzureCloudImages(BaseUseCase):
    STORAGE_KEY_NAME = "AZURE_STORAGE_CONNECTION_STRING"

    def __init__(
        self, response: Response, container: str, folder_name: str, download_path: str,
    ):
        super().__init__(response)
        self._container = container
        self._folder_name = folder_name
        self._download_path = download_path

    @property
    def get_blobs(self):
        bucket = self.client.get_container_client(self._container)
        return bucket.list_blobs(name_starts_with=self._folder_name)

    @property
    def connect_key(self):
        return os.getenv(self.STORAGE_KEY_NAME)

    @property
    def client(self):
        return BlobServiceClient.from_connection_string(self.connect_key)

    def execute(self):
        blob_client = self.client
        image_blobs = self.get_blobs()
        downloaded_images = []
        duplicated_images = []
        failed_images = []
        path = Path(self._download_path)
        for image_blob in image_blobs:
            if image_blobs.content_type.startswith("image"):
                image_name = os.path.basename(image_blob.name)
                image_path = path / image_name
                if image_name not in downloaded_images:
                    try:
                        image_blob_client = blob_client.get_blob_client(
                            container=self._container, blob=image_blob
                        )
                        image_stream = image_blob_client.download_blob()
                    except AzureError as e:
                        self._response.errors = (
                            f"Couldn't download image {image_name} {e}"
                        )
                        failed_images.append(image_name)
                    else:
                        with open(image_path, "wb") as image_file:
                            image_file.write(image_stream.readall())
                        downloaded_images.append(image_name)
                else:
                    duplicated_images.append(image_name)

        self._response.data = {
            "downloaded_images": downloaded_images,
            "duplicated_images": duplicated_images,
            "failed_images": failed_images,
        }


class GetProjectMetadataUseCase(BaseUseCase):
    def __init__(
        self,
        project: ProjectEntity,
        response: Response,
        service: SuerannotateServiceProvider,
        annotation_classes: BaseManageableRepository,
        settings: BaseManageableRepository,
        workflows: BaseManageableRepository,
        projects: BaseManageableRepository,
        include_annotation_classes: bool,
        include_settings: bool,
        include_workflow: bool,
        include_contributors: bool,
        include_complete_image_count: bool,
    ):
        super().__init__(response)
        self._project = project
        self._response = response
        self._service = service

        self._annotation_classes = annotation_classes
        self._settings = settings
        self._workflows = workflows
        self._projects = projects

        self._include_annotation_classes = include_annotation_classes
        self._include_settings = include_settings
        self._include_workflow = include_workflow
        self._include_contributors = include_contributors
        self._include_complete_image_count = include_complete_image_count

        self._annotation_classes_response = Response()
        self._settings_response = Response()
        self._workflows_response = Response()

    @property
    def annotation_classes_use_case(self):
        return GetAnnotationClassesUseCase(
            response=self._annotation_classes_response, classes=self._annotation_classes
        )

    @property
    def settings_use_case(self):
        return GetSettingsUseCase(
            response=self._settings_response, settings=self._settings
        )

    @property
    def work_flow_use_case(self):
        return GetWorkflowsUseCase(
            response=self._workflows_response, workflows=self._workflows
        )

    def execute(self):
        data = {"project": self._project}

        if self._include_annotation_classes:
            self.annotation_classes_use_case.execute()
            data["classes"] = self._annotation_classes_response.data

        if self._include_settings:
            self.settings_use_case.execute()
            data["settings"] = self._settings_response.data

        if self._include_workflow:
            self.work_flow_use_case.execute()
            data["workflows"] = self._settings_response.data

        if self._include_contributors:
            data["contributors"] = self._project.users

        if self._include_complete_image_count:
            projects = self._projects.get_all(
                condition=(
                    Condition("completeImagesCount", "true", EQ)
                    & Condition("name", self._project.name, EQ)
                    & Condition("team_id", self._project.team_id, EQ)
                )
            )
            if projects:
                data["project"] = projects[0]

        self._response.data = data


class GetImageAnnotationsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        image_name: str,
        images: BaseManageableRepository,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._folder = folder
        self._image_name = image_name
        self._image_response = Response()
        self._images = images

    @property
    def image_use_case(self):
        return GetImageUseCase(
            response=self._image_response,
            project=self._project,
            folder=self._folder,
            image_name=self._image_name,
            images=self._images,
        )

    def execute(self):
        data = {
            "annotation_json": None,
            "annotation_json_filename": None,
            "annotation_mask": None,
            "annotation_mask_filename": None,
        }
        self.image_use_case.execute()
        token = self._service.get_download_token(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
            image_id=self._image_response.data.uuid,
        )
        credentials = token["annotations"]["MAIN"][0]
        annotation_json_creds = credentials["annotation_json_path"]
        if self._project.project_type == constances.ProjectType.VECTOR.value:
            file_postfix = "___objects.json"
        else:
            file_postfix = "___pixel.json"

        response = requests.get(
            url=annotation_json_creds["url"], headers=annotation_json_creds["headers"],
        )
        if not response.ok:
            raise AppException(f"Couldn't load annotations {response.text}")
        data["annotation_json"] = response.json()
        data["annotation_json_filename"] = f"{self._image_name}{file_postfix}.json"
        if self._project.project_type == constances.ProjectType.PIXEL.value:
            annotation_blue_map_creds = credentials["annotation_bluemap_path"]
            response = requests.get(
                url=annotation_blue_map_creds["url"],
                headers=annotation_blue_map_creds["headers"],
            )
            data["annotation_mask"] = io.BytesIO(response.content)
            data["annotation_mask_filename"] = f"{self._image_name}___save.png"

        self._response.data = data


class GetS3ImageUseCase(BaseUseCase):
    def __init__(
        self, response: Response, s3_bucket, image_path: str,
    ):
        super().__init__(response)
        self._s3_bucket = s3_bucket
        self._image_path = image_path

    def execute(self):
        image = io.BytesIO()
        session = boto3.Session()
        resource = session.resource("s3")
        image_object = resource.Object(self._s3_bucket, self._image_path)
        if image_object.content_length > constances.MAX_IMAGE_SIZE:
            raise AppValidationException(f"File size is {image_object.content_length}")
        image_object.download_fileobj(image)
        self._response.data = image


class GetImagePreAnnotationsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        image_name: str,
        images: BaseManageableRepository,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._folder = folder
        self._image_name = image_name
        self._image_response = Response()
        self._images = images

    @property
    def get_image_use_case(self):
        return GetImageUseCase(
            response=self._image_response,
            project=self._project,
            folder=self._folder,
            image_name=self._image_name,
            images=self._images,
        )

    def execute(self):
        data = {
            "preannotation_json": None,
            "preannotation_json_filename": None,
            "preannotation_mask": None,
            "preannotation_mask_filename": None,
        }
        self.get_image_use_case.execute()
        token = self._service.get_download_token(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
            image_id=self._image_response.data.uuid,
        )
        credentials = token["annotations"]["PREANNOTATION"][0]
        annotation_json_creds = credentials["annotation_json_path"]
        if self._project.project_type == constances.ProjectType.VECTOR.value:
            file_postfix = "___objects.json"
        else:
            file_postfix = "___pixel.json"

        response = requests.get(
            url=annotation_json_creds["url"], headers=annotation_json_creds["headers"],
        )
        if not response.ok:
            raise AppException(f"Couldn't load annotations {response.text}")
        data["preannotation_json"] = response.json()
        data["preannotation_json_filename"] = f"{self._image_name}{file_postfix}.json"
        if self._project.project_type == constances.ProjectType.PIXEL.value:
            annotation_blue_map_creds = credentials["annotation_bluemap_path"]
            response = requests.get(
                url=annotation_blue_map_creds["url"],
                headers=annotation_blue_map_creds["headers"],
            )
            data["preannotation_mask"] = io.BytesIO(response.content)
            data["preannotation_mask_filename"] = f"{self._image_name}___save.png"

        self._response.data = data


class DownloadImageAnnotationsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        image_name: str,
        images: BaseManageableRepository,
        destination: str,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._folder = folder
        self._image_name = image_name
        self._image_response = Response()
        self._images = images
        self._destination = destination

    @property
    def image_use_case(self):
        return GetImageUseCase(
            response=self._image_response,
            project=self._project,
            folder=self._folder,
            image_name=self._image_name,
            images=self._images,
        )

    def execute(self):
        data = {
            "annotation_json": None,
            "annotation_json_filename": None,
            "annotation_mask": None,
            "annotation_mask_filename": None,
        }
        self.image_use_case.execute()
        token = self._service.get_download_token(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
            image_id=self._image_response.data.uuid,
        )
        credentials = token["annotations"]["MAIN"][0]

        annotation_json_creds = credentials["annotation_json_path"]
        if self._project.project_type == constances.ProjectType.VECTOR.value:
            file_postfix = "___objects.json"
        else:
            file_postfix = "___pixel.json"

        response = requests.get(
            url=annotation_json_creds["url"], headers=annotation_json_creds["headers"],
        )
        if not response.ok:
            raise AppException(f"Couldn't load annotations {response.text}")
        data["annotation_json"] = response.json()
        data["annotation_json_filename"] = f"{self._image_name}{file_postfix}"
        if self._project.project_type == constances.ProjectType.PIXEL.value:
            annotation_blue_map_creds = credentials["annotation_bluemap_path"]
            response = requests.get(
                url=annotation_blue_map_creds["url"],
                headers=annotation_blue_map_creds["headers"],
            )
            data["annotation_mask"] = io.BytesIO(response.content)
            data["annotation_mask_filename"] = f"{self._image_name}___save.png"
            mask_path = Path(self._destination) / data["annotation_mask_filename"]
            with open(mask_path, "wb") as f:
                f.write(data["annotation_mask"].getbuffer())

        json_path = Path(self._destination) / data["annotation_json_filename"]
        with open(json_path, "w") as f:
            json.dump(data["annotation_json"], f, indent=4)

        self._response.data = (str(json_path), str(mask_path))


class DownloadImagePreAnnotationsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        image_name: str,
        images: BaseManageableRepository,
        destination: str,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._folder = folder
        self._image_name = image_name
        self._image_response = Response()
        self._images = images
        self._destination = destination

    @property
    def image_use_case(self):
        return GetImageUseCase(
            response=self._image_response,
            project=self._project,
            folder=self._folder,
            image_name=self._image_name,
            images=self._images,
        )

    def execute(self):
        data = {
            "preannotation_json": None,
            "preannotation_json_filename": None,
            "preannotation_mask": None,
            "preannotation_mask_filename": None,
        }
        self.image_use_case.execute()
        token = self._service.get_download_token(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
            image_id=self._image_response.data.uuid,
        )
        credentials = token["annotations"]["PREANNOTATION"][0]
        annotation_json_creds = credentials["annotation_json_path"]
        if self._project.project_type == constances.ProjectType.VECTOR.value:
            file_postfix = "___objects.json"
        else:
            file_postfix = "___pixel.json"

        response = requests.get(
            url=annotation_json_creds["url"], headers=annotation_json_creds["headers"],
        )
        if not response.ok:
            raise AppException(f"Couldn't load annotations {response.text}")
        data["preannotation_json"] = response.json()
        data["preannotation_json_filename"] = f"{self._image_name}{file_postfix}"
        if self._project.project_type == constances.ProjectType.PIXEL.value:
            annotation_blue_map_creds = credentials["annotation_bluemap_path"]
            response = requests.get(
                url=annotation_blue_map_creds["url"],
                headers=annotation_blue_map_creds["headers"],
            )
            data["preannotation_mask"] = io.BytesIO(response.content)
            data["preannotation_mask_filename"] = f"{self._image_name}___save.png"
            mask_path = Path(self._destination) / data["preannotation_mask_filename"]
            with open(mask_path, "wb") as f:
                f.write(data["preannotation_mask"].getbuffer())

        json_path = Path(self._destination) / data["preannotation_json_filename"]
        with open(json_path, "w") as f:
            json.dump(data["preannotation_json"], f, indent=4)

        self._response.data = (str(json_path), str(mask_path))


class GetExportsUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        return_metadata: bool = False,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._return_metadata = return_metadata

    def execute(self):
        data = self._service.get_exports(
            team_id=self._project.team_id, project_id=self._project.uuid
        )
        self._response.data = data
        if not self._return_metadata:
            self._response.data = [i["name"] for i in data]


class UploadS3ImagesBackendUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        backend_service_provider: SuerannotateServiceProvider,
        settings: BaseReadOnlyRepository,
        project: ProjectEntity,
        folder: FolderEntity,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        folder_path: str,
        image_quality: str,
    ):
        super().__init__(response)
        self._backend_service = backend_service_provider
        self._settings = settings
        self._project = project
        self._folder = folder
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket_name = bucket_name
        self._folder_path = folder_path
        self._image_quality = image_quality

    def validate_image_quality(self):
        if self._image_quality and self._image_quality not in (
            "compressed",
            "original",
        ):
            raise AppValidationException("Invalid value for image_quality")

    def execute(self):
        old_setting = None
        if self._image_quality:
            settings = self._settings.get_all()
            for setting in settings:
                if setting.attribute == "ImageQuality":
                    if setting.value == "compressed":
                        setting.value = 60
                    else:
                        setting.value = 100
                    self._backend_service.set_project_settings(
                        project_id=self._project.uuid,
                        team_id=self._project.team_id,
                        data=[setting.to_dict()],
                    )
                    break
            else:
                raise AppException("Cant find settings.")

        in_progress = self._backend_service.upload_form_s3(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            access_key=self._access_key,
            secret_key=self._secret_key,
            bucket_name=self._bucket_name,
            from_folder_name=self._folder_path,
            to_folder_id=self._folder.uuid,
        )
        if in_progress:
            while True:
                time.sleep(4)
                progress = self._backend_service.get_upload_status(
                    project_id=self._project.uuid,
                    team_id=self._project.team_id,
                    folder_id=self._folder.uuid,
                )
                if progress == "2":
                    break
                elif progress == "1":
                    raise AppException("Couldn't upload to project from S3.")

        if old_setting:
            self._backend_service.set_project_settings(
                project_id=self._project.uuid,
                team_id=self._project.team_id,
                data=[old_setting.to_dict()],
            )


class GetProjectImageCountUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        service: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        with_all_subfolders: bool = False,
    ):
        super().__init__(response)
        self._service = service
        self._project = project
        self._folder = folder
        self._with_all_subfolders = with_all_subfolders

    def execute(self):
        data = self._service.get_project_images_count(
            project_id=self._project.uuid, team_id=self._project.team_id
        )
        count = 0
        if self._with_all_subfolders:
            for i in data["data"]:
                count += i["imagesCount"]
        else:
            for i in data["data"]:
                if i["name"] == self._folder.name:
                    count = i["imagesCount"]
        self._response.data = count


class UploadVideoUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        project: ProjectEntity,
        folder: FolderEntity,
        settings: BaseManageableRepository,
        s3_repo: BaseManageableRepository,
        video_path: str,
        start_time: float,
        end_time: float = None,
        annotation_status_code: int = constances.AnnotationStatus.NOT_STARTED.value,
        image_quality_in_editor: str = None,
    ):
        super().__init__(response)
        self._project = project
        self._folder = folder
        self._settings = settings
        self._s3_repo = s3_repo
        self._video_path = video_path
        self._start_time = start_time
        self._end_time = end_time
        self._annotation_status_code = annotation_status_code
        self._image_quality_in_editor = image_quality_in_editor

    def get_upload_s3_use_case(self, image, image_path, upload_path):
        return UploadImageS3UseCas(
            response=Response(),
            project=self._project,
            project_settings=self._settings,
            image_path=image_path,
            image=image,
            s3_repo=self._s3_repo,
            upload_path=upload_path,
        )

    def execute(self):
        pass


class ExtractFramesUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        backend_service_provider: SuerannotateServiceProvider,
        project: ProjectEntity,
        folder: FolderEntity,
        video_path: str,
        extract_path: str,
        start_time: float,
        end_time: float = None,
        target_fps: float = None,
        annotation_status_code: int = constances.AnnotationStatus.NOT_STARTED.value,
        image_quality_in_editor: str = None,
    ):
        super().__init__(response)
        self._backend_service = backend_service_provider
        self._project = project
        self._folder = folder
        self._video_path = video_path
        self._extract_path = extract_path
        self._start_time = start_time
        self._end_time = end_time
        self._target_fps = target_fps
        self._annotation_status_code = annotation_status_code
        self._image_quality_in_editor = image_quality_in_editor

    @property
    def upload_auth_data(self):
        return self._backend_service.get_s3_upload_auth_token(
            project_id=self._project.uuid,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
        )

    @property
    def limit(self):
        return self.upload_auth_data.get("availableImageCount")

    def execute(self):
        extracted_paths = VideoPlugin.extract_frames(
            video_path=self._video_path,
            start_time=self._start_time,
            end_time=self._end_time,
            extract_path=self._extract_path,
            limit=self.limit,
            target_fps=self._target_fps,
        )
        self._response.data = extracted_paths


class CreateAnnotationClassUseCase(BaseUseCase):
    def __init__(
        self,
        response: Response,
        annotation_classes: BaseManageableRepository,
        annotation_class: AnnotationClassEntity,
    ):
        super().__init__(response)
        self._annotation_classes = annotation_classes
        self._annotation_class = annotation_class

    def execute(self):
        created = self._annotation_classes.insert(entity=self._annotation_class)
        self._response.data = created
