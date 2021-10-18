from typing import List
from typing import Optional
from typing import Union

from pydantic import BaseModel
from pydantic import constr
from pydantic import Extra
from pydantic import StrictStr
from pydantic import validate_model
from pydantic import ValidationError
from pydantic import validator


NotEmptyStr = constr(strict=True, min_length=1)


class AnnotationType(StrictStr):
    @classmethod
    def validate(cls, value: str) -> Union[str]:
        if value not in ANNOTATION_TYPES.keys():
            raise TypeError(f"Invalid type {value}")
        return value


class Attribute(BaseModel):
    name: NotEmptyStr


class AttributeGroup(BaseModel):
    name: StrictStr
    is_multiselect: Optional[int] = False
    attributes: List[Attribute]


class ClassesJson(BaseModel):
    name: StrictStr
    color: StrictStr
    attribute_groups: List[AttributeGroup]


class Metadata(BaseModel):
    name: Optional[NotEmptyStr]
    width: Optional[int]
    height: Optional[int]


class BaseInstance(BaseModel):
    type: AnnotationType
    classId: int
    groupId: Optional[int]
    attributes: List[Attribute]

    class Config:
        error_msg_templates = {
            'value_error.missing': 'field required for annotation',
        }


class Point(BaseInstance):
    x: float
    y: float


class PolyLine(BaseInstance):
    points: List[float]


class Polygon(BaseInstance):
    points: List[float]


class BboxPoints(BaseModel):
    x1: float
    x2: float
    y1: float
    y2: float


class Bbox(BaseInstance):
    points: BboxPoints


class Ellipse(BaseInstance):
    cx: float
    cy: float
    rx: float
    ry: float


class TemplatePoint(BaseModel):
    id: int
    x: float
    y: float


class TemplateConnection(BaseModel):
    id: int
    to: int


class Template(BaseInstance):
    points: List[TemplatePoint]
    connections: List[Optional[TemplateConnection]]
    templateId: int


class EmptyPoint(BaseModel):
    x: float
    y: float


class CuboidPoint(BaseModel):
    f1: EmptyPoint
    f2: EmptyPoint
    r1: EmptyPoint
    r2: EmptyPoint


class Cuboid(BaseInstance):
    points: CuboidPoint


class PixelAnnotationPart(BaseModel):
    color: NotEmptyStr


class PixelAnnotationInstance(BaseModel):
    classId: int
    groupId: Optional[int]
    parts: List[PixelAnnotationPart]
    attributes: List[Attribute]


class VectorInstance(BaseModel):
    __root__: Union[Template, Cuboid, Point, PolyLine, Polygon, Bbox, Ellipse]


ANNOTATION_TYPES = {
    "bbox": Bbox,
    "ellipse": Ellipse,
    "template": Template,
    "cuboid": Cuboid,
    "polyline": PolyLine,
    "polygon": Polygon,
}


class VectorAnnotation(BaseModel):
    metadata: Metadata
    instances: Optional[List[Union[Template, Cuboid, Point, PolyLine, Polygon, Bbox, Ellipse]]]

    @validator("instances", pre=True, each_item=True)
    def check_instances(cls, instance):
        # todo add type checking
        annotation_type = AnnotationType.validate(instance.get("type"))
        result = validate_model(ANNOTATION_TYPES[annotation_type], instance)
        if result[2]:
            raise ValidationError(result[2].raw_errors, model=ANNOTATION_TYPES[annotation_type])
        return instance


class PixelAnnotation(BaseModel):
    metadata: Metadata
    instances: List[PixelAnnotationInstance]


class Project(BaseModel):
    name: NotEmptyStr

    class Config:
        extra = Extra.allow


class MLModel(BaseModel):
    name: NotEmptyStr
    id: int
    path: NotEmptyStr
    config_path: NotEmptyStr
    team_id: Optional[int]

    class Config:
        extra = Extra.allow
