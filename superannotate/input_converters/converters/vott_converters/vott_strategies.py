import json
from pathlib import Path

from .vott_to_sa_vector import vott_to_sa

from ..baseStrategy import baseStrategy
from ....common import dump_output


class VoTTStrategy(baseStrategy):
    name = "VoTT converter"

    def __init__(self, args):
        super().__init__(args)
        self.conversion_algorithm = vott_to_sa

    def __str__(self):
        return '{} object'.format(self.name)

    def to_sa_format(self):
        json_data = self.get_file_list()
        sa_jsons, sa_classes = self.conversion_algorithm(json_data, self.task)
        dump_output(self.output_dir, self.platform, sa_classes, sa_jsons)

    def get_file_list(self):
        json_file_list = []
        path = Path(self.export_root)
        if self.dataset_name != '':
            json_file_list.append(path.joinpath(self.dataset_name + '.json'))
        else:
            file_generator = path.glob('*.json')
            for gen in file_generator:
                json_file_list.append(gen)

        return json_file_list
