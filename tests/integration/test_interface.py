import os
from os.path import dirname
import tempfile

import src.superannotate as sa
from src.superannotate.lib.app.exceptions import AppException
from tests.integration.base import BaseTestCase


class TestInterface(BaseTestCase):
    PROJECT_NAME = "Interface test"
    TEST_FOLDER_PATH = "data_set/sample_project_vector"
    TEST_FOLDER_PATH_WITH_MULTIPLE_IMAGERS = "data_set/sample_project_vector"
    PROJECT_DESCRIPTION = "desc"
    PROJECT_TYPE = "Vector"
    TEST_FOLDER_NAME = "folder"
    EXAMPLE_IMAGE_1 = "example_image_1.jpg"
    EXAMPLE_IMAGE_2 = "example_image_2.jpg"

    @property
    def folder_path(self):
        return os.path.join(dirname(dirname(__file__)), self.TEST_FOLDER_PATH)

    @property
    def folder_path_with_multiple_images(self):
        return os.path.join(dirname(dirname(__file__)), self.TEST_FOLDER_PATH_WITH_MULTIPLE_IMAGERS)

    def test_delete_images(self):
        sa.create_folder(self.PROJECT_NAME, self.TEST_FOLDER_NAME)

        sa.upload_images_from_folder_to_project(
            f"{self.PROJECT_NAME}/{self.TEST_FOLDER_NAME}",
            self.folder_path,
            annotation_status="InProgress",
        )
        num_images = sa.get_project_image_count(
            self.PROJECT_NAME, with_all_subfolders=True
        )
        self.assertEqual(num_images, 4)
        sa.delete_images(f"{self.PROJECT_NAME}/{self.TEST_FOLDER_NAME}")

        num_images = sa.get_project_image_count(
            self.PROJECT_NAME, with_all_subfolders=True
        )
        self.assertEqual(num_images, 0)

    def test_delete_folder(self):
        with self.assertRaises(AppException):
            sa.delete_folders(self.PROJECT_NAME, ["non-existing folder"])

    def test_get_project_metadata(self):
        metadata = sa.get_project_metadata(self.PROJECT_NAME)
        self.assertIsNotNone(metadata["id"])
        self.assertListEqual(metadata.get("contributors", []), [])
        metadata_with_users = sa.get_project_metadata(self.PROJECT_NAME, include_contributors=True)
        self.assertIsNotNone(metadata_with_users.get("contributors"))

    def test_upload_annotations_from_folder_to_project(self):
        sa.upload_images_from_folder_to_project(
            self.PROJECT_NAME,
            self.folder_path,
            annotation_status="Completed",
        )
        uploaded_annotations, _, _ = sa.upload_annotations_from_folder_to_project(
            self.PROJECT_NAME, self.folder_path
        )
        self.assertEqual(len(uploaded_annotations), 4)

    def test_get_images_metadata(self):
        sa.upload_images_from_folder_to_project(self.PROJECT_NAME, self.folder_path)
        metadata = sa.search_images(self.PROJECT_NAME, self.EXAMPLE_IMAGE_1, return_metadata=True)
        self.assertIn("qa_id", metadata[0])

    def test_download_image_annotations(self):
        sa.upload_images_from_folder_to_project(self.PROJECT_NAME, self.folder_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            sa.download_image_annotations(self.PROJECT_NAME, self.EXAMPLE_IMAGE_1, temp_dir)

    def test_search_folder(self):
        team_users = sa.search_team_contributors()
        sa.share_project(self.PROJECT_NAME, team_users[0], "QA")
        sa.create_folder(self.PROJECT_NAME, self.TEST_FOLDER_NAME)
        data = sa.search_folders(self.PROJECT_NAME, return_metadata=True)
        folder_data = sa.search_folders(self.PROJECT_NAME, self.TEST_FOLDER_NAME, return_metadata=True)
        self.assertEqual(data, folder_data)

    def test_get_project_settings(self):
        sa.set_project_settings(self.PROJECT_NAME, [{'attribute': 'ImageQuality', 'value': 'original'}])
        data = sa.get_project_settings(self.PROJECT_NAME)
        for elem in data:
            if elem["attribute"] == "ImageQuality":
                self.assertEqual(elem["value"], "original")
                break

    def test_search_project(self):
        sa.upload_images_from_folder_to_project(self.PROJECT_NAME, self.folder_path)
        sa.set_image_annotation_status(self.PROJECT_NAME, self.EXAMPLE_IMAGE_1, "Completed")
        data = sa.search_projects(self.PROJECT_NAME, return_metadata=True, include_complete_image_count=True)
        self.assertIsNotNone(data[0]['completed_images_count'])
