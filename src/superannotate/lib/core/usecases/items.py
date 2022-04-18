import copy
from typing import List

import superannotate.lib.core as constances
from lib.core.conditions import Condition
from lib.core.conditions import CONDITION_EQ as EQ
from lib.core.entities import AttachmentEntity
from lib.core.entities import DocumentEntity
from lib.core.entities import Entity
from lib.core.entities import FolderEntity
from lib.core.entities import ProjectEntity
from lib.core.entities import TmpBaseEntity
from lib.core.entities import TmpImageEntity
from lib.core.entities import VideoEntity
from lib.core.exceptions import AppException
from lib.core.exceptions import AppValidationException
from lib.core.exceptions import BackendError
from lib.core.reporter import Reporter
from lib.core.repositories import BaseReadOnlyRepository
from lib.core.response import Response
from lib.core.serviceproviders import SuperannotateServiceProvider
from lib.core.usecases.base import BaseReportableUseCae
from pydantic import parse_obj_as


class GetItem(BaseReportableUseCae):
    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        folder: FolderEntity,
        items: BaseReadOnlyRepository,
        item_name: str,
    ):
        super().__init__(reporter)
        self._project = project
        self._folder = folder
        self._items = items
        self._item_name = item_name

    @staticmethod
    def serialize_entity(entity: Entity, project: ProjectEntity):
        if project.upload_state != constances.UploadState.EXTERNAL.value:
            entity.url = None
        if project.type in (
            constances.ProjectType.VECTOR.value,
            constances.ProjectType.PIXEL.value,
        ):
            tmp_entity = entity
            if project.type == constances.ProjectType.VECTOR.value:
                entity.segmentation_status = None
            if project.upload_state == constances.UploadState.EXTERNAL.value:
                tmp_entity.prediction_status = None
                tmp_entity.segmentation_status = None
            return TmpImageEntity(**tmp_entity.dict(by_alias=True))
        elif project.type == constances.ProjectType.VIDEO.value:
            return VideoEntity(**entity.dict(by_alias=True))
        elif project.type == constances.ProjectType.DOCUMENT.value:
            return DocumentEntity(**entity.dict(by_alias=True))
        return entity

    def execute(self) -> Response:
        if self.is_valid():
            condition = (
                Condition("name", self._item_name, EQ)
                & Condition("team_id", self._project.team_id, EQ)
                & Condition("project_id", self._project.id, EQ)
                & Condition("folder_id", self._folder.uuid, EQ)
            )
            entity = self._items.get_one(condition)
            if entity:
                entity.add_path(self._project.name, self._folder.name)
                self._response.data = self.serialize_entity(entity, self._project)
            else:
                self._response.errors = AppException("Item not found.")
        return self._response


class QueryEntities(BaseReportableUseCae):
    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        folder: FolderEntity,
        backend_service_provider: SuperannotateServiceProvider,
        query: str,
    ):
        super().__init__(reporter)
        self._project = project
        self._folder = folder
        self._backend_client = backend_service_provider
        self._query = query

    def validate_query(self):
        response = self._backend_client.validate_saqul_query(
            self._project.team_id, self._project.id, self._query
        )
        if response.get("error"):
            raise AppException(response["error"])
        if response["isValidQuery"]:
            self._query = response["parsedQuery"]
        else:
            raise AppException("Incorrect query.")
        if self._project.sync_status != constances.ProjectState.SYNCED.value:
            raise AppException("Data is not synced.")

    def execute(self) -> Response:
        if self.is_valid():
            service_response = self._backend_client.saqul_query(
                self._project.team_id,
                self._project.id,
                self._query,
                folder_id=None if self._folder.name == "root" else self._folder.uuid,
            )
            if service_response.ok:
                data = parse_obj_as(
                    List[TmpBaseEntity],
                    [Entity.map_fields(i) for i in service_response.data],
                )
                for i, item in enumerate(data):
                    data[i] = GetItem.serialize_entity(item, self._project)
                self._response.data = data
            else:
                self._response.errors = service_response.data
        return self._response


class ListItems(BaseReportableUseCae):
    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        folder: FolderEntity,
        items: BaseReadOnlyRepository,
        search_condition: Condition,
        folders: BaseReadOnlyRepository,
        recursive: bool = False,
    ):
        super().__init__(reporter)
        self._project = project
        self._folder = folder
        self._items = items
        self._folders = folders
        self._search_condition = search_condition
        self._recursive = recursive

    def validate_recursive_case(self):
        if not self._folder.is_root and self._recursive:
            self._recursive = False

    def execute(self) -> Response:
        if self.is_valid():
            self._search_condition &= Condition("team_id", self._project.team_id, EQ)
            self._search_condition &= Condition("project_id", self._project.id, EQ)

            if not self._recursive:
                self._search_condition &= Condition("folder_id", self._folder.uuid, EQ)
                items = [
                    GetItem.serialize_entity(
                        item.add_path(self._project.name, self._folder.name),
                        self._project,
                    )
                    for item in self._items.get_all(self._search_condition)
                ]
            else:
                items = []
                folders = self._folders.get_all(
                    Condition("team_id", self._project.team_id, EQ)
                    & Condition("project_id", self._project.id, EQ),
                )
                folders.append(self._folder)
                for folder in folders:
                    tmp = self._items.get_all(
                        copy.deepcopy(self._search_condition)
                        & Condition("folder_id", folder.uuid, EQ)
                    )
                    items.extend(
                        [
                            GetItem.serialize_entity(
                                item.add_path(self._project.name, folder.name),
                                self._project,
                            )
                            for item in tmp
                        ]
                    )
            self._response.data = items
        return self._response


class AttachItems(BaseReportableUseCae):
    CHUNK_SIZE = 500

    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        folder: FolderEntity,
        attachments: List[AttachmentEntity],
        annotation_status: str,
        backend_service_provider: SuperannotateServiceProvider,
        upload_state_code: int = constances.UploadState.EXTERNAL.value,
    ):
        super().__init__(reporter)
        self._project = project
        self._folder = folder
        self._attachments = attachments
        self._annotation_status_code = constances.AnnotationStatus.get_value(
            annotation_status
        )
        self._upload_state_code = upload_state_code
        self._backend_service = backend_service_provider
        self._attachments_count = None

    @property
    def attachments_count(self):
        if not self._attachments_count:
            self._attachments_count = len(self._attachments)
        return self._attachments_count

    def validate_limitations(self):
        attachments_count = self.attachments_count
        response = self._backend_service.get_limitations(
            team_id=self._project.team_id,
            project_id=self._project.id,
            folder_id=self._folder.uuid,
        )
        if not response.ok:
            raise AppValidationException(response.error)
        if attachments_count > response.data.folder_limit.remaining_image_count:
            raise AppValidationException(constances.ATTACH_FOLDER_LIMIT_ERROR_MESSAGE)
        elif attachments_count > response.data.project_limit.remaining_image_count:
            raise AppValidationException(constances.ATTACH_PROJECT_LIMIT_ERROR_MESSAGE)
        elif (
            response.data.user_limit
            and attachments_count > response.data.user_limit.remaining_image_count
        ):
            raise AppValidationException(constances.ATTACH_USER_LIMIT_ERROR_MESSAGE)

    def validate_upload_state(self):
        if self._project.upload_state == constances.UploadState.BASIC.value:
            raise AppValidationException(constances.ATTACHING_UPLOAD_STATE_ERROR)

    @staticmethod
    def generate_meta():
        return {"width": None, "height": None}

    def execute(self) -> Response:
        if self.is_valid():
            duplications = []
            attached = []
            self.reporter.start_progress(self.attachments_count, "Attaching URLs")
            for i in range(0, self.attachments_count, self.CHUNK_SIZE):
                attachments = self._attachments[i : i + self.CHUNK_SIZE]  # noqa: E203
                response = self._backend_service.get_bulk_images(
                    project_id=self._project.id,
                    team_id=self._project.team_id,
                    folder_id=self._folder.uuid,
                    images=[attachment.name for attachment in attachments],
                )
                if isinstance(response, dict) and "error" in response:
                    raise AppException(response["error"])
                duplications.extend([image["name"] for image in response])
                to_upload = []
                to_upload_meta = {}
                for attachment in attachments:
                    if attachment.name not in duplications:
                        to_upload.append(
                            {"name": attachment.name, "path": attachment.url}
                        )
                        to_upload_meta[attachment.name] = self.generate_meta()
                if to_upload:
                    backend_response = self._backend_service.attach_files(
                        project_id=self._project.id,
                        folder_id=self._folder.uuid,
                        team_id=self._project.team_id,
                        files=to_upload,
                        annotation_status_code=self._annotation_status_code,
                        upload_state_code=self._upload_state_code,
                        meta=to_upload_meta,
                    )
                    if "error" in backend_response:
                        self._response.errors = AppException(backend_response["error"])
                    else:
                        attached.extend(backend_response)
                self.reporter.update_progress(len(attachments))
            self.reporter.finish_progress()
            self._response.data = attached, duplications
        return self._response


class CopyItems(BaseReportableUseCae):
    """
    Copy items in bulk between folders in a project.
    Return skipped item names.
    """

    CHUNK_SIZE = 1000

    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        from_folder: FolderEntity,
        to_folder: FolderEntity,
        item_names: List[str],
        items: BaseReadOnlyRepository,
        backend_service_provider: SuperannotateServiceProvider,
        include_annotations: bool,
    ):
        super().__init__(reporter)
        self._project = project
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._item_names = item_names
        self._items = items
        self._backend_service = backend_service_provider
        self._include_annotations = include_annotations

    def _validate_limitations(self, items_count):
        response = self._backend_service.get_limitations(
            team_id=self._project.team_id,
            project_id=self._project.id,
            folder_id=self._to_folder.uuid,
        )
        if not response.ok:
            raise AppValidationException(response.error)
        if items_count > response.data.folder_limit.remaining_image_count:
            raise AppValidationException(constances.COPY_FOLDER_LIMIT_ERROR_MESSAGE)
        if items_count > response.data.project_limit.remaining_image_count:
            raise AppValidationException(constances.COPY_PROJECT_LIMIT_ERROR_MESSAGE)

    def validate_item_names(self):
        if self._item_names:
            self._item_names = list(set(self._item_names))

    def execute(self):
        if self.is_valid():
            if self._item_names:
                items = self._item_names
            else:
                condition = (
                    Condition("team_id", self._project.team_id, EQ)
                    & Condition("project_id", self._project.id, EQ)
                    & Condition("folder_id", self._from_folder.uuid, EQ)
                )
                items = [item.name for item in self._items.get_all(condition)]

            existing_items = self._backend_service.get_bulk_images(
                project_id=self._project.id,
                team_id=self._project.team_id,
                folder_id=self._to_folder.uuid,
                images=items,
            )
            duplications = [item["name"] for item in existing_items]
            items_to_copy = list(set(items) - set(duplications))
            skipped_items = duplications
            try:
                self._validate_limitations(len(items_to_copy))
            except AppValidationException as e:
                self._response.errors = e
                return self._response
            if items_to_copy:
                for i in range(0, len(items_to_copy), self.CHUNK_SIZE):
                    chunk_to_copy = items_to_copy[i : i + self.CHUNK_SIZE]  # noqa: E203
                    poll_id = self._backend_service.copy_items_between_folders_transaction(
                        team_id=self._project.team_id,
                        project_id=self._project.id,
                        from_folder_id=self._from_folder.uuid,
                        to_folder_id=self._to_folder.uuid,
                        items=chunk_to_copy,
                        include_annotations=self._include_annotations,
                    )
                    if not poll_id:
                        skipped_items.extend(chunk_to_copy)
                        continue
                    try:
                        self._backend_service.await_progress(
                            self._project.id,
                            self._project.team_id,
                            poll_id=poll_id,
                            items_count=len(chunk_to_copy),
                        )
                    except BackendError as e:
                        self._response.errors = AppException(e)
                        return self._response
                existing_items = self._backend_service.get_bulk_images(
                    project_id=self._project.id,
                    team_id=self._project.team_id,
                    folder_id=self._to_folder.uuid,
                    images=items,
                )
                existing_item_names_set = {item["name"] for item in existing_items}
                items_to_copy_names_set = set(items_to_copy)
                copied_items = existing_item_names_set.intersection(
                    items_to_copy_names_set
                )
                skipped_items.extend(list(items_to_copy_names_set - copied_items))
                self.reporter.log_info(
                    f"Copied {len(copied_items)}/{len(items)} item(s) from "
                    f"{self._project.name}{'' if self._from_folder.is_root else f'/{self._from_folder.name}'} to "
                    f"{self._project.name}{'' if self._to_folder.is_root else f'/{self._to_folder.name}'}"
                )
            self._response.data = skipped_items
        return self._response


class MoveItems(BaseReportableUseCae):
    CHUNK_SIZE = 1000

    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        from_folder: FolderEntity,
        to_folder: FolderEntity,
        item_names: List[str],
        items: BaseReadOnlyRepository,
        backend_service_provider: SuperannotateServiceProvider,
    ):
        super().__init__(reporter)
        self._project = project
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._item_names = item_names
        self._items = items
        self._backend_service = backend_service_provider

    def validate_item_names(self):
        if self._item_names:
            self._item_names = list(set(self._item_names))

    def _validate_limitations(self, items_count):
        response = self._backend_service.get_limitations(
            team_id=self._project.team_id,
            project_id=self._project.id,
            folder_id=self._to_folder.uuid,
        )
        if not response.ok:
            raise AppValidationException(response.error)
        if items_count > response.data.folder_limit.remaining_image_count:
            raise AppValidationException(constances.MOVE_FOLDER_LIMIT_ERROR_MESSAGE)
        if items_count > response.data.project_limit.remaining_image_count:
            raise AppValidationException(constances.MOVE_PROJECT_LIMIT_ERROR_MESSAGE)

    def execute(self):
        if self.is_valid():
            if not self._item_names:
                condition = (
                    Condition("team_id", self._project.team_id, EQ)
                    & Condition("project_id", self._project.id, EQ)
                    & Condition("folder_id", self._from_folder.uuid, EQ)
                )
                items = [item.name for item in self._items.get_all(condition)]
            else:
                items = self._item_names
            try:
                self._validate_limitations(len(items))
            except AppValidationException as e:
                self._response.errors = e
                return self._response
            moved_images = []
            for i in range(0, len(items), self.CHUNK_SIZE):
                moved_images.extend(
                    self._backend_service.move_images_between_folders(
                        team_id=self._project.team_id,
                        project_id=self._project.id,
                        from_folder_id=self._from_folder.uuid,
                        to_folder_id=self._to_folder.uuid,
                        images=items[i : i + self.CHUNK_SIZE],  # noqa: E203
                    )
                )
            self.reporter.log_info(
                f"Moved {len(moved_images)}/{len(items)} item(s) from "
                f"{self._project.name}{'' if self._from_folder.is_root else f'/{self._from_folder.name}'} to "
                f"{self._project.name}{'' if self._to_folder.is_root else f'/{self._to_folder.name}'}"
            )

            self._response.data = list(set(items) - set(moved_images))
        return self._response


class SetAnnotationStatues(BaseReportableUseCae):
    CHUNK_SIZE = 500
    ERROR_MESSAGE = "Failed to change status"

    def __init__(
        self,
        reporter: Reporter,
        project: ProjectEntity,
        folder: FolderEntity,
        items: BaseReadOnlyRepository,
        annotation_status: str,
        backend_service_provider: SuperannotateServiceProvider,
        item_names: List[str] = None,
    ):
        super().__init__(reporter)
        self._project = project
        self._folder = folder
        self._item_names = item_names
        self._items = items
        self._annotation_status_code = constances.AnnotationStatus.get_value(
            annotation_status
        )
        self._backend_service = backend_service_provider

    def validate_items(self):
        if not self._item_names:
            condition = (
                Condition("team_id", self._project.team_id, EQ)
                & Condition("project_id", self._project.id, EQ)
                & Condition("folder_id", self._folder.uuid, EQ)
            )
            self._item_names = [item.name for item in self._items.get_all(condition)]
            return
        existing_items = self._backend_service.get_bulk_images(
            project_id=self._project.id,
            team_id=self._project.team_id,
            folder_id=self._folder.uuid,
            images=self._item_names,
        )
        if not existing_items:
            raise AppValidationException(self.ERROR_MESSAGE)
        if existing_items:
            self._item_names = list(
                {i["name"] for i in existing_items}.intersection(set(self._item_names))
            )

    def execute(self):
        if self.is_valid():
            for i in range(0, len(self._item_names), self.CHUNK_SIZE):
                status_changed = self._backend_service.set_images_statuses_bulk(
                    image_names=self._item_names[
                        i : i + self.CHUNK_SIZE
                    ],  # noqa: E203,
                    team_id=self._project.team_id,
                    project_id=self._project.id,
                    folder_id=self._folder.uuid,
                    annotation_status=self._annotation_status_code,
                )
                if not status_changed:
                    self._response.errors = AppException(self.ERROR_MESSAGE)
                    break
        return self._response
