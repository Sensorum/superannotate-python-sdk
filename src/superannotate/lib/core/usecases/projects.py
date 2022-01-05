import copy
import logging
from collections import defaultdict
from typing import Iterable
from typing import List
from typing import Type

import lib.core as constances
from lib.core.conditions import Condition
from lib.core.conditions import CONDITION_EQ as EQ
from lib.core.entities import AnnotationClassEntity
from lib.core.entities import FolderEntity
from lib.core.entities import ProjectEntity
from lib.core.entities import ProjectSettingEntity
from lib.core.entities import TeamEntity
from lib.core.entities import WorkflowEntity
from lib.core.exceptions import AppException
from lib.core.exceptions import AppValidationException
from lib.core.reporter import Reporter
from lib.core.repositories import BaseManageableRepository
from lib.core.repositories import BaseReadOnlyRepository
from lib.core.serviceproviders import SuerannotateServiceProvider
from lib.core.usecases.base import BaseReportableUseCae
from lib.core.usecases.base import BaseUseCase
from lib.core.usecases.base import BaseUserBasedUseCase
from requests.exceptions import RequestException

logger = logging.getLogger("root")


class GetProjectsUseCase(BaseUseCase):
    def __init__(
            self, condition: Condition, team_id: int, projects: BaseManageableRepository,
    ):
        super().__init__()
        self._condition = condition
        self._projects = projects
        self._team_id = team_id

    def execute(self):
        if self.is_valid():
            condition = self._condition & Condition("team_id", self._team_id, EQ)
            self._response.data = self._projects.get_all(condition)
        return self._response


class GetProjectByNameUseCase(BaseUseCase):
    def __init__(
            self, name: str, team_id: int, projects: BaseManageableRepository,
    ):
        super().__init__()
        self._name = name
        self._projects = projects
        self._team_id = team_id

    def execute(self):
        if self.is_valid():
            condition = Condition("name", self._name, EQ) & Condition(
                "team_id", self._team_id, EQ
            )
            projects = self._projects.get_all(condition)
            if not projects:
                self._response.errors = AppException("Project not found.")
            else:
                project = next(
                    (project for project in projects if project.name == self._name),
                    None,
                )
                if not project:
                    self._response.errors = AppException("Project not found")
                self._response.data = project
        return self._response


class GetProjectMetaDataUseCase(BaseUseCase):
    def __init__(
            self,
            project: ProjectEntity,
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
        super().__init__()
        self._project = project
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

    @property
    def annotation_classes_use_case(self):
        return GetAnnotationClassesUseCase(classes=self._annotation_classes)

    @property
    def settings_use_case(self):
        return GetSettingsUseCase(settings=self._settings)

    @property
    def work_flow_use_case(self):
        return GetWorkflowsUseCase(
            project=self._project,
            workflows=self._workflows,
            annotation_classes=self._annotation_classes,
        )

    def execute(self):
        data = {}
        project = self._projects.get_one(
            uuid=self._project.uuid, team_id=self._project.team_id
        )
        data["project"] = project
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

        if self._include_annotation_classes:
            self.annotation_classes_use_case.execute()
            data["classes"] = self.annotation_classes_use_case.execute().data

        if self._include_settings:
            self.settings_use_case.execute()
            data["settings"] = self.settings_use_case.execute().data

        if self._include_workflow:
            self.work_flow_use_case.execute()
            data["workflows"] = self.work_flow_use_case.execute().data

        if self._include_contributors:
            data["contributors"] = project.users
        else:
            project.users = []

        self._response.data = data
        return self._response


class CreateProjectUseCase(BaseUseCase):
    def __init__(
            self,
            project: ProjectEntity,
            projects: BaseManageableRepository,
            backend_service_provider: SuerannotateServiceProvider,
            settings_repo: Type[BaseManageableRepository],
            annotation_classes_repo: BaseManageableRepository,
            workflows_repo: BaseManageableRepository,
            settings: List[ProjectSettingEntity] = None,
            workflows: List[WorkflowEntity] = None,
            annotation_classes: List[AnnotationClassEntity] = None,
            contributors: Iterable[dict] = None,
    ):

        super().__init__()
        self._project = project
        self._projects = projects
        self._settings = settings
        self._settings_repo = settings_repo
        self._annotation_classes_repo = annotation_classes_repo
        self._workflows_repo = workflows_repo
        self._workflows = workflows
        self._annotation_classes = annotation_classes
        self._contributors = contributors
        self._backend_service = backend_service_provider

    def validate_project_name(self):
        if (
                len(
                    set(self._project.name).intersection(
                        constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                    )
                )
                > 0
        ):
            self._project.name = "".join(
                "_"
                if char in constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                else char
                for char in self._project.name
            )
            logger.warning(
                "New folder name has special characters. Special characters will be replaced by underscores."
            )
        condition = Condition("name", self._project.name, EQ) & Condition(
            "team_id", self._project.team_id, EQ
        )
        for project in self._projects.get_all(condition):
            if project.name == self._project.name:
                logger.error("There are duplicated names.")
                raise AppValidationException(
                    f"Project name {self._project.name} is not unique. "
                    f"To use SDK please make project names unique."
                )

    def validate_description(self):
        if not self._project.description:
            raise AppValidationException("Please provide a project description.")

    def execute(self):
        if self.is_valid():
            # TODO add status in the constants
            self._project.status = 0
            entity = self._projects.insert(self._project)
            self._response.data = entity
            data = {}
            if self._settings:
                settings_repo = self._settings_repo(self._backend_service, entity)
                for setting in self._settings:
                    for new_setting in settings_repo.get_all():
                        if new_setting.attribute == setting.attribute:
                            setting_copy = copy.copy(setting)
                            setting_copy.uuid = new_setting.uuid
                            setting_copy.project_id = entity.uuid
                            settings_repo.update(setting_copy)
                data["settings"] = self._settings
            annotation_classes_mapping = {}
            if self._annotation_classes:
                annotation_repo = self._annotation_classes_repo(
                    self._backend_service, entity
                )
                for annotation_class in self._annotation_classes:
                    annotation_classes_mapping[
                        annotation_class.uuid
                    ] = annotation_repo.insert(annotation_class)
                self._response.data.annotation_classes = self._annotation_classes
            if self._workflows:
                workflow_repo = self._workflows_repo(self._backend_service, entity)
                for workflow in self._workflows:
                    workflow.project_id = entity.uuid
                    workflow.class_id = annotation_classes_mapping.get(
                        workflow.class_id
                    )
                    workflow_repo.insert(workflow)
                data["workflows"] = self._workflows

            if self._contributors:
                for contributor in self._contributors:
                    self._backend_service.share_project(
                        entity.uuid,
                        entity.team_id,
                        contributor["user_id"],
                        constances.UserRole.get_value(contributor["user_role"]),
                    )
                data["contributors"] = self._contributors

            logger.info(
                "Created project %s (ID %s) with type %s",
                self._response.data.name,
                self._response.data.uuid,
                constances.ProjectType.get_name(self._response.data.project_type),
            )
        return self._response


class DeleteProjectUseCase(BaseUseCase):
    def __init__(
            self, project_name: str, team_id: int, projects: BaseManageableRepository,
    ):

        super().__init__()
        self._project_name = project_name
        self._team_id = team_id
        self._projects = projects

    def execute(self):
        use_case = GetProjectByNameUseCase(
            name=self._project_name, team_id=self._team_id, projects=self._projects,
        )
        project_response = use_case.execute()
        if project_response.data:
            is_deleted = self._projects.delete(project_response.data)
            if is_deleted:
                logger.info("Successfully deleted project ")
            else:
                raise AppException("Couldn't delete project")


class UpdateProjectUseCase(BaseUseCase):
    def __init__(
            self,
            project: ProjectEntity,
            project_data: dict,
            projects: BaseManageableRepository,
    ):

        super().__init__()
        self._project = project
        self._project_data = project_data
        self._projects = projects

    def validate_project_name(self):
        if self._project_data.get("name"):
            if (
                    len(
                        set(self._project_data["name"]).intersection(
                            constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                        )
                    )
                    > 0
            ):
                self._project_data["name"] = "".join(
                    "_"
                    if char in constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                    else char
                    for char in self._project_data["name"]
                )
                logger.warning(
                    "New folder name has special characters. Special characters will be replaced by underscores."
                )
            condition = Condition("name", self._project_data["name"], EQ) & Condition(
                "team_id", self._project.team_id, EQ
            )
            for project in self._projects.get_all(condition):
                if project.name == self._project_data["name"]:
                    logger.error("There are duplicated names.")
                    raise AppValidationException(
                        f"Project name {self._project_data['name']} is not unique. "
                        f"To use SDK please make project names unique."
                    )

    def execute(self):
        if self.is_valid():
            for field, value in self._project_data.items():
                setattr(self._project, field, value)
            new_project = self._projects.update(self._project)
            self._response.data = new_project.to_dict()
        return self._response


class CloneProjectUseCase(BaseReportableUseCae):
    def __init__(
            self,
            reporter: Reporter,
            project: ProjectEntity,
            project_to_create: ProjectEntity,
            projects: BaseManageableRepository,
            settings_repo: Type[BaseManageableRepository],
            workflows_repo: Type[BaseManageableRepository],
            annotation_classes_repo: Type[BaseManageableRepository],
            backend_service_provider: SuerannotateServiceProvider,
            include_annotation_classes: bool = True,
            include_settings: bool = True,
            include_workflow: bool = True,
            include_contributors: bool = False,
    ):
        super().__init__(reporter)
        self._project = project
        self._project_to_create = project_to_create
        self._projects = projects
        self._settings_repo = settings_repo
        self._workflows_repo = workflows_repo
        self._annotation_classes_repo = annotation_classes_repo
        self._backend_service = backend_service_provider
        self._include_annotation_classes = include_annotation_classes
        self._include_settings = include_settings
        self._include_workflow = include_workflow
        self._include_contributors = include_contributors

    @property
    def annotation_classes(self):
        return self._annotation_classes_repo(self._backend_service, self._project)

    @property
    def settings(self):
        return self._settings_repo(self._backend_service, self._project)

    @property
    def workflows(self):
        return self._workflows_repo(self._backend_service, self._project)

    def validate_project_name(self):
        if self._project_to_create.name:
            if (
                    len(
                        set(self._project_to_create.name).intersection(
                            constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                        )
                    )
                    > 0
            ):
                self._project_to_create.name = "".join(
                    "_"
                    if char in constances.SPECIAL_CHARACTERS_IN_PROJECT_FOLDER_NAMES
                    else char
                    for char in self._project_to_create.name
                )
                logger.warning(
                    "New folder name has special characters. Special characters will be replaced by underscores."
                )
            condition = Condition("name", self._project_to_create.name, EQ) & Condition(
                "team_id", self._project.team_id, EQ
            )
            for project in self._projects.get_all(condition):
                if project.name == self._project_to_create.name:
                    logger.error("There are duplicated names.")
                    raise AppValidationException(
                        f"Project name {self._project_to_create.name} is not unique. "
                        f"To use SDK please make project names unique."
                    )

    def get_annotation_classes_repo(self, project: ProjectEntity):
        return self._annotation_classes_repo(self._backend_service, project)

    def _copy_annotation_classes(
            self, annotation_classes_entity_mapping: dict, project: ProjectEntity
    ):
        annotation_classes = self.annotation_classes.get_all()
        for annotation_class in annotation_classes:
            annotation_class_copy = copy.copy(annotation_class)
            annotation_classes_entity_mapping[
                annotation_class.uuid
            ] = self.get_annotation_classes_repo(project).insert(annotation_class_copy)

    def _copy_include_contributors(self, to_project: ProjectEntity):
        from_project = self._projects.get_one(
            uuid=self._project.uuid, team_id=self._project.team_id
        )
        users = []
        for user in from_project.users:
            users.append(
                {"user_id": user.get("user_id"), "user_role": user.get("user_role")}
            )

        for user in from_project.unverified_users:
            users.append(
                {"user_id": user.get("email"), "user_role": user.get("user_role")}
            )
        if users:
            self._backend_service.share_project_bulk(
                to_project.uuid, to_project.team_id, users
            )

    def _copy_settings(self, to_project: ProjectEntity):
        new_settings = self._settings_repo(self._backend_service, to_project)
        for setting in self.settings.get_all():
            for new_setting in new_settings.get_all():
                if new_setting.attribute == setting.attribute:
                    setting_copy = copy.copy(setting)
                    setting_copy.uuid = new_setting.uuid
                    setting_copy.project_id = to_project.uuid
                    new_settings.update(setting_copy)

    def _copy_workflow(
            self, annotation_classes_entity_mapping: dict, to_project: ProjectEntity
    ):
        new_workflows = self._workflows_repo(self._backend_service, to_project)
        for workflow in self.workflows.get_all():
            existing_workflow_ids = list(map(lambda i: i.uuid, new_workflows.get_all()))
            workflow_data = copy.copy(workflow)
            workflow_data.project_id = to_project.uuid
            workflow_data.class_id = annotation_classes_entity_mapping[
                workflow.class_id
            ].uuid
            new_workflows.insert(workflow_data)
            workflows = new_workflows.get_all()
            new_workflow = next(
                (
                    work_flow
                    for work_flow in workflows
                    if work_flow.uuid not in existing_workflow_ids
                ),
                None,
            )
            workflow_attributes = []
            for attribute in workflow_data.attribute:
                for annotation_attribute in annotation_classes_entity_mapping[
                    workflow.class_id
                ].attribute_groups:
                    if (
                            attribute["attribute"]["attribute_group"]["name"]
                            == annotation_attribute["name"]
                    ):
                        for annotation_attribute_value in annotation_attribute[
                            "attributes"
                        ]:
                            if (
                                    annotation_attribute_value["name"]
                                    == attribute["attribute"]["name"]
                            ):
                                workflow_attributes.append(
                                    {
                                        "workflow_id": new_workflow.uuid,
                                        "attribute_id": annotation_attribute_value[
                                            "id"
                                        ],
                                    }
                                )
                                break
            if workflow_attributes:
                self._backend_service.set_project_workflow_attributes_bulk(
                    project_id=to_project.uuid,
                    team_id=to_project.team_id,
                    attributes=workflow_attributes,
                )

    def execute(self):
        if self.is_valid():
            project = self._projects.insert(self._project_to_create)
            self.reporter.log_info(
                f"Created project {self._project_to_create.name} with type"
                f" {constances.ProjectType.get_name(self._project_to_create.project_type)}."
            )
            annotation_classes_entity_mapping = defaultdict(AnnotationClassEntity)
            annotation_classes_created = False
            if self._include_annotation_classes:
                self.reporter.log_info(
                    f"Cloning annotation classes from {self._project.name} to {self._project_to_create.name}."
                )
                try:
                    self._copy_annotation_classes(
                        annotation_classes_entity_mapping, project
                    )
                    annotation_classes_created = True
                except (AppException, RequestException) as e:
                    self.reporter.log_warning(
                        f"Failed to clone annotation classes from {self._project.name} to {self._project_to_create.name}."
                    )
                    self.reporter.log_debug(str(e), exc_info=True)

            if self._include_settings:
                self.reporter.log_info(
                    f"Cloning settings from {self._project.name} to {self._project_to_create.name}."
                )
                try:
                    self._copy_settings(project)
                except (AppException, RequestException) as e:
                    self.reporter.log_warning(
                        f"Failed to clone settings from {self._project.name} to {self._project_to_create.name}."
                    )
                    self.reporter.log_debug(str(e), exc_info=True)

            if self._include_workflow:
                if self._project.project_type in (
                        constances.ProjectType.DOCUMENT.value,
                        constances.ProjectType.VIDEO.value,
                ):
                    self.reporter.log_warning(
                        "Workflow copy is deprecated for "
                        f"{constances.ProjectType.get_name(self._project_to_create.project_type)} projects."
                    )
                elif not annotation_classes_created:
                    self.reporter.log_info(
                        f"Skipping the workflow clone from {self._project.name} to {self._project_to_create.name}."
                    )
                else:
                    self.reporter.log_info(
                        f"Cloning workflow from {self._project.name} to {self._project_to_create.name}."
                    )
                    try:
                        self._copy_workflow(annotation_classes_entity_mapping, project)
                    except (AppException, RequestException) as e:
                        self.reporter.log_warning(
                            f"Failed to workflow from {self._project.name} to {self._project_to_create.name}."
                        )
                        self.reporter.log_debug(str(e), exc_info=True)
            if self._include_contributors:
                self.reporter.log_info(
                    f"Cloning contributors from {self._project.name} to {self._project_to_create.name}."
                )
                try:
                    self._copy_include_contributors(project)
                except (AppException, RequestException) as e:
                    self.reporter.log_warning(
                        f"Failed to clone contributors from {self._project.name} to {self._project_to_create.name}."
                    )
                    self.reporter.log_debug(str(e), exc_info=True)
            self._response.data = self._projects.get_one(
                uuid=project.uuid, team_id=project.team_id
            )

        return self._response


class ShareProjectUseCase(BaseUseCase):
    def __init__(
            self,
            service: SuerannotateServiceProvider,
            project_entity: ProjectEntity,
            user_id: str,
            user_role: str,
    ):
        super().__init__()
        self._service = service
        self._project_entity = project_entity
        self._user_id = user_id
        self._user_role = user_role

    @property
    def user_role(self):
        return constances.UserRole.get_value(self._user_role)

    def execute(self):
        self._response.data = self._service.share_project_bulk(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            users=[{"user_id": self._user_id, "user_role": self.user_role}],
        )
        if not self._response.errors:
            logger.info(
                f"Shared project {self._project_entity.name} with user {self._user_id} and role {constances.UserRole.get_name(self.user_role)}"
            )
        return self._response


class UnShareProjectUseCase(BaseUseCase):
    def __init__(
            self,
            service: SuerannotateServiceProvider,
            project_entity: ProjectEntity,
            user_id: str,
    ):
        super().__init__()
        self._service = service
        self._project_entity = project_entity
        self._user_id = user_id

    def execute(self):
        self._response.data = self._service.un_share_project(
            team_id=self._project_entity.team_id,
            project_id=self._project_entity.uuid,
            user_id=self._user_id,
        )
        logger.info(
            f"Unshared project {self._project_entity.name} from user ID {self._user_id}"
        )
        return self._response


class GetSettingsUseCase(BaseUseCase):
    def __init__(self, settings: BaseManageableRepository):
        super().__init__()
        self._settings = settings

    def execute(self):
        self._response.data = self._settings.get_all()
        return self._response


class GetWorkflowsUseCase(BaseUseCase):
    def __init__(
            self,
            project: ProjectEntity,
            annotation_classes: BaseReadOnlyRepository,
            workflows: BaseManageableRepository,
            fill_classes=True,
    ):
        super().__init__()
        self._project = project
        self._workflows = workflows
        self._annotation_classes = annotation_classes
        self._fill_classes = fill_classes

    def validate_project_type(self):
        if self._project.project_type in constances.LIMITED_FUNCTIONS:
            raise AppValidationException(
                constances.LIMITED_FUNCTIONS[self._project.project_type]
            )

    def execute(self):
        if self.is_valid():
            data = []
            workflows = self._workflows.get_all()
            for workflow in workflows:
                workflow_data = workflow.to_dict()
                if self._fill_classes:
                    annotation_classes = self._annotation_classes.get_all()
                    for annotation_class in annotation_classes:
                        annotation_class.uuid = workflow.class_id
                        workflow_data["className"] = annotation_class.name
                data.append(workflow_data)
            self._response.data = data
        return self._response


class GetAnnotationClassesUseCase(BaseUseCase):
    def __init__(
            self, classes: BaseManageableRepository, condition: Condition = None,
    ):
        super().__init__()
        self._classes = classes
        self._condition = condition

    def execute(self):
        self._response.data = self._classes.get_all(condition=self._condition)
        return self._response


class UpdateSettingsUseCase(BaseUseCase):
    def __init__(
            self,
            projects: BaseReadOnlyRepository,
            settings: BaseManageableRepository,
            to_update: List,
            backend_service_provider: SuerannotateServiceProvider,
            project_id: int,
            team_id: int,
    ):
        super().__init__()
        self._projects = projects
        self._settings = settings
        self._to_update = to_update
        self._backend_service_provider = backend_service_provider
        self._project_id = project_id
        self._team_id = team_id

    def validate_image_quality(self):
        for setting in self._to_update:
            if setting["attribute"].lower() == "imagequality" and isinstance(
                    setting["value"], str
            ):
                setting["value"] = constances.ImageQuality.get_value(setting["value"])
                return

    def validate_project_type(self):
        project = self._projects.get_one(uuid=self._project_id, team_id=self._team_id)
        for attribute in self._to_update:
            if attribute.get(
                    "attribute", ""
            ) == "ImageQuality" and project.project_type in [
                constances.ProjectType.VIDEO.value,
                constances.ProjectType.DOCUMENT.value,
            ]:
                raise AppValidationException(
                    constances.DEPRICATED_DOCUMENT_VIDEO_MESSAGE
                )

    def execute(self):
        if self.is_valid():
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
        return self._response


class GetProjectImageCountUseCase(BaseUseCase):
    def __init__(
            self,
            service: SuerannotateServiceProvider,
            project: ProjectEntity,
            folder: FolderEntity,
            with_all_sub_folders: bool = False,
    ):
        super().__init__()
        self._service = service
        self._project = project
        self._folder = folder
        self._with_all_sub_folders = with_all_sub_folders

    def validate_user_input(self):
        if not self._folder.name == "root" and self._with_all_sub_folders:
            raise AppValidationException("The folder does not contain any sub-folders.")

    def validate_project_type(self):
        if self._project.project_type in constances.LIMITED_FUNCTIONS:
            raise AppValidationException(
                constances.LIMITED_FUNCTIONS[self._project.project_type]
            )

    def execute(self):
        if self.is_valid():
            data = self._service.get_project_images_count(
                project_id=self._project.uuid, team_id=self._project.team_id
            )
            count = 0
            if self._folder.name == "root":
                count += data["images"]["count"]
                if self._with_all_sub_folders:
                    for i in data["folders"]["data"]:
                        count += i["imagesCount"]
            else:
                for i in data["folders"]["data"]:
                    if i["id"] == self._folder.uuid:
                        count = i["imagesCount"]

            self._response.data = count
        return self._response


class SetWorkflowUseCase(BaseUseCase):
    def __init__(
            self,
            service: SuerannotateServiceProvider,
            annotation_classes_repo: BaseManageableRepository,
            workflow_repo: BaseManageableRepository,
            steps: list,
            project: ProjectEntity,
    ):
        super().__init__()
        self._service = service
        self._annotation_classes_repo = annotation_classes_repo
        self._workflow_repo = workflow_repo
        self._steps = steps
        self._project = project

    def validate_project_type(self):
        if self._project.project_type in constances.LIMITED_FUNCTIONS:
            raise AppValidationException(
                constances.LIMITED_FUNCTIONS[self._project.project_type]
            )

    def execute(self):
        if self.is_valid():
            annotation_classes = self._annotation_classes_repo.get_all()
            annotation_classes_map = {}
            annotations_classes_attributes_map = {}
            for annotation_class in annotation_classes:
                annotation_classes_map[annotation_class.name] = annotation_class.uuid
                for attribute_group in annotation_class.attribute_groups:
                    for attribute in attribute_group["attributes"]:
                        annotations_classes_attributes_map[
                            f"{annotation_class.name}__{attribute_group['name']}__{attribute['name']}"
                        ] = attribute["id"]

            for step in [step for step in self._steps if "className" in step]:
                if step.get("id"):
                    del step["id"]
                step["class_id"] = annotation_classes_map.get(step["className"], None)
                if not step["class_id"]:
                    raise AppException(
                        "Annotation class not found in set_project_workflow."
                    )

            self._service.set_project_workflow_bulk(
                team_id=self._project.team_id,
                project_id=self._project.uuid,
                steps=self._steps,
            )
            existing_workflows = self._workflow_repo.get_all()
            existing_workflows_map = {}
            for workflow in existing_workflows:
                existing_workflows_map[workflow.step] = workflow.uuid

            req_data = []
            for step in self._steps:
                annotation_class_name = step["className"]
                for attribute in step["attribute"]:
                    attribute_name = attribute["attribute"]["name"]
                    attribute_group_name = attribute["attribute"]["attribute_group"][
                        "name"
                    ]
                    if not annotations_classes_attributes_map.get(
                            f"{annotation_class_name}__{attribute_group_name}__{attribute_name}",
                            None,
                    ):
                        raise AppException(
                            "Attribute group name or attribute name not found in set_project_workflow."
                        )

                    if not existing_workflows_map.get(step["step"], None):
                        raise AppException("Couldn't find step in workflow")

                    req_data.append(
                        {
                            "workflow_id": existing_workflows_map[step["step"]],
                            "attribute_id": annotations_classes_attributes_map[
                                f"{annotation_class_name}__{attribute_group_name}__{attribute_name}"
                            ],
                        }
                    )

            self._service.set_project_workflow_attributes_bulk(
                project_id=self._project.uuid,
                team_id=self._project.team_id,
                attributes=req_data,
            )
        return self._response


class GetTeamUseCase(BaseUseCase):
    def __init__(self, teams: BaseReadOnlyRepository, team_id: int):
        super().__init__()
        self._teams = teams
        self._team_id = team_id

    def execute(self):
        try:
            self._response.data = self._teams.get_one(self._team_id)
        except Exception:
            raise AppException("Can't get team data.") from None
        return self._response


class SearchContributorsUseCase(BaseUseCase):
    def __init__(
            self,
            backend_service_provider: SuerannotateServiceProvider,
            team_id: int,
            condition: Condition = None,
    ):
        super().__init__()
        self._backend_service = backend_service_provider
        self._team_id = team_id
        self._condition = condition

    @property
    def condition(self):
        if self._condition:
            return self._condition.build_query()

    def execute(self):
        res = self._backend_service.search_team_contributors(
            self._team_id, self.condition
        )
        self._response.data = res
        return self._response


class AddContributorsToProject(BaseUserBasedUseCase):
    """
    Returns tuple of lists (added, skipped)
    """

    def __init__(
            self,
            reporter: Reporter,
            team: TeamEntity,
            project: ProjectEntity,
            emails: list,
            role: str,
            service: SuerannotateServiceProvider,
    ):
        super().__init__(reporter, emails)
        self._team = team
        self._project = project
        self._role = role
        self._service = service

    @property
    def user_role(self):
        return constances.UserRole.get_value(self._role)

    def execute(self):
        if self.is_valid():
            team_users = set()
            project_users = {user["user_id"] for user in self._project.users}
            for user in self._team.users:
                if user.user_role > constances.UserRole.ADMIN.value:
                    team_users.add(user.email)
            # collecting pending team users which is not admin
            for user in self._team.pending_invitations:
                if user["user_role"] > constances.UserRole.ADMIN.value:
                    team_users.add(user["email"])
            # collecting pending project users which is not admin
            for user in self._project.unverified_users:
                if user["user_role"] > constances.UserRole.ADMIN.value:
                    project_users.add(user["email"])

            to_add = list(team_users.intersection(self._emails) - project_users)
            to_skip = list(set(self._emails).difference(to_add))

            if to_skip:
                self.reporter.log_warning(
                    f"Skipped {len(to_skip)}/{len(self._emails)} "
                    "contributors that are out of the team scope or already have access to the project."
                )
            if to_add:
                response = self._service.share_project_bulk(
                    team_id=self._team.uuid,
                    project_id=self._project.uuid,
                    users=[
                        dict(user_id=user_id, user_role=self.user_role)
                        for user_id in to_add
                    ],
                )
                if response and not response.get("invalidUsers"):
                    self.reporter.log_info(
                        f"Added {len(to_add)}/{len(self._emails)} "
                        f"contributors to the project {self._project.name} with the {self._role} role."
                    )
            self._response.data = to_add, to_skip
            return self._response


class InviteContributorsToTeam(BaseUserBasedUseCase):
    """
    Returns tuple of lists (added, skipped)
    """

    def __init__(
            self,
            reporter: Reporter,
            team: TeamEntity,
            emails: list,
            set_admin: bool,
            service: SuerannotateServiceProvider,
    ):
        super().__init__(reporter, emails)
        self._team = team
        self._set_admin = set_admin
        self._service = service

    def execute(self):
        if self.is_valid():
            team_users = {user.email for user in self._team.users}
            # collecting pending team users
            team_users.update({user["email"] for user in self._team.pending_invitations})

            emails = set(self._emails)

            to_skip = list(emails.intersection(team_users))
            to_add = list(emails.difference(to_skip))
            invited, failed = [], to_skip
            if to_skip:
                self.reporter.log_warning(
                    f"Found {len(to_skip)}/{len(self._emails)} existing members of the team."
                )
            if to_add:
                invited, failed = self._service.invite_contributors(
                    team_id=self._team.uuid,
                    # REMINDER UserRole.VIEWER is the contributor for the teams
                    team_role=constances.UserRole.ADMIN.value if self._set_admin else constances.UserRole.VIEWER.value,
                    emails=to_add
                )
                if invited:
                    self.reporter.log_info(
                        f"Sent team {'admin' if self._set_admin else 'contributor'} invitations"
                        f" to {len(to_add)}/{len(self._emails)} users."
                    )
                if failed:
                    to_skip = set(to_skip).update(set(failed))
                    self.reporter.log_info(
                        f"Skipped team {'admin' if self._set_admin else 'contributor'} "
                        f"invitations for {len(failed)}/{len(self._emails)} users."
                    )
            self._response.data = invited, list(to_skip)
            return self._response
