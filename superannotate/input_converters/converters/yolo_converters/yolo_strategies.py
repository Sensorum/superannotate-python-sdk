import json

from ..baseStrategy import baseStrategy
from .yolo_to_sa_vector import yolo_object_detection_to_sa_vector

from ....common import dump_output


class YoloStrategy(baseStrategy):
    name = "Yolo converter"

    def __init__(self, args):
        super().__init__(args)
        self.__setup_conversion_algorithm()

    def __setup_conversion_algorithm(self):
        if self.direction == "from":
            if self.project_type == "Vector":
                if self.task == "object_detection":
                    self.converion_algorithm = yolo_object_detection_to_sa_vector

    def to_sa_format(self):
        sa_jsons, sa_classes = self.converion_algorithm(self.export_root)
        dump_output(self.output_dir, self.platform, sa_classes, sa_jsons)
