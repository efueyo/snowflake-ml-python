from typing import TYPE_CHECKING, Sequence, TypedDict, TypeVar, Union

import numpy.typing as npt
from typing_extensions import NotRequired

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd
    import sklearn.base
    import sklearn.pipeline

    import snowflake.ml.model.custom_model


_SupportedBuiltins = Union[int, float, bool, str, bytes, "_SupportedBuiltinsList"]
_SupportedNumpyDtype = Union[
    "np.int16",
    "np.int32",
    "np.int64",
    "np.float32",
    "np.float64",
    "np.uint16",
    "np.uint32",
    "np.uint64",
    "np.bool8",
    "np.str0",
    "np.bytes0",
]
_SupportedNumpyArray = npt.NDArray[_SupportedNumpyDtype]
_SupportedBuiltinsList = Sequence[_SupportedBuiltins]

SupportedDataType = Union["pd.DataFrame", _SupportedNumpyArray, Sequence[_SupportedNumpyArray], _SupportedBuiltinsList]

CustomModelType = TypeVar("CustomModelType", bound="snowflake.ml.model.custom_model.CustomModel")

ModelType = Union[
    "snowflake.ml.model.custom_model.CustomModel", "sklearn.base.BaseEstimator", "sklearn.pipeline.Pipeline"
]
"""This is defined as the type that Snowflake native model packaging could accept.
To avoid importing a lot of modules that users might not need too earlier by defining this Union type,
we have to use all string-based typing hints here.

Here is all acceptable types of Snowflake native model packaging and its handler file in _handlers/ folder.

| Type                            | Handler File | Handler             |
|---------------------------------|--------------|---------------------|
| snowflake.ml.model.custom_model.CustomModel | custom.py    | _CustomModelHandler |
| sklearn.base.BaseEstimator      | sklearn.py   | _SKLModelHandler    |
| sklearn.pipeline.Pipeline       | sklearn.py   | _SKLModelHandler    |
"""


class DeployOptions(TypedDict):
    _snowml_wheel_path: NotRequired[str]


class WarehouseDeployOptions(DeployOptions):
    """Options for deploying to the Snowflake Warehouse.

    relax_version: Whether or not relax the version constraints of the dependencies if unresolvable. Defaults to False.
    keep_order: Whether or not preserve the row order when predicting. Only available for dataframe has fewer than 2**64
        rows. Defaults to True.
    """

    relax_version: NotRequired[bool]
    keep_order: NotRequired[bool]


class ModelSaveOption(TypedDict):
    ...


class CustomModelSaveOption(TypedDict):
    ...


class SKLModelSaveOptions(ModelSaveOption):
    target_methods: NotRequired[Sequence[str]]
