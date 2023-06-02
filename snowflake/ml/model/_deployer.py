import json
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Optional, TypedDict, Union, overload

import numpy as np
import pandas as pd
from typing_extensions import Required

from snowflake.ml._internal.utils import identifier
from snowflake.ml.model import _udf_util, model_signature, type_hints as model_types
from snowflake.snowpark import DataFrame as SnowparkDataFrame, Session, functions as F
from snowflake.snowpark._internal import type_utils


class TargetPlatform(Enum):
    WAREHOUSE = "warehouse"


class Deployment(TypedDict):
    """Deployment information.

    Attributes:
        name: Name of the deployment.
        platform: Target platform to deploy the model.
        signature: The signature of the model method.
        options: Additional options when deploying the model.
    """

    name: Required[str]
    platform: Required[TargetPlatform]
    signature: model_signature.ModelSignature
    options: Required[model_types.DeployOptions]


class DeploymentManager(ABC):
    """WIP: Intended to provide model deployment management.
    Abstract class for a deployment manager.
    """

    @abstractmethod
    def create(
        self,
        name: str,
        platform: TargetPlatform,
        signature: model_signature.ModelSignature,
        options: Optional[model_types.DeployOptions] = None,
    ) -> Deployment:
        """Create a deployment.

        Args:
            name: Name of the deployment for the model.
            platform: Target platform to deploy the model.
            signature: The signature of the model method.
            options: Additional options when deploying the model.
                Each target platform will have their own specifications of options.
        """
        pass

    @abstractmethod
    def list(self) -> List[Deployment]:
        """List all deployment in this manager."""
        pass

    @abstractmethod
    def get(self, name: str) -> Optional[Deployment]:
        """Get a specific deployment with the given name in this manager.

        Args:
            name: Name of deployment.
        """
        pass

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete a deployment with the given name in this manager.

        Args:
            name: Name of deployment.
        """
        pass


class LocalDeploymentManager(DeploymentManager):
    """A simplest implementation of Deployment Manager that store the deployment information locally."""

    def __init__(self) -> None:
        self._storage: Dict[str, Deployment] = dict()

    def create(
        self,
        name: str,
        platform: TargetPlatform,
        signature: model_signature.ModelSignature,
        options: Optional[model_types.DeployOptions] = None,
    ) -> Deployment:
        """Create a deployment.

        Args:
            name: Name of the deployment for the model.
            platform: Target platform to deploy the model.
            signature: The signature of the model method.
            options: Additional options when deploying the model.
                Each target platform will have their own specifications of options.

        Returns:
            The deployment information.
        """
        if not options:
            options = {}
        info = Deployment(
            name=name,
            platform=platform,
            signature=signature,
            options=options,
        )
        self._storage[name] = info
        return info

    def list(self) -> List[Deployment]:
        """List all deployments.

        Returns:
            A list of stored deployments information.
        """
        return list(self._storage.values())

    def get(self, name: str) -> Optional[Deployment]:
        """Get a specific deployment with the given name if exists.

        Args:
            name: Name of deployment.

        Returns:
            The deployment information. Return None if the requested deployment does not exist.
        """
        if name in self._storage:
            return self._storage[name]
        else:
            return None

    def delete(self, name: str) -> None:
        """Delete a deployment with the given name.

        Args:
            name: Name of deployment.
        """
        self._storage.pop(name)


class Deployer:
    """A deployer that deploy a model to the remote. Currently only deploying to the warehouse is supported.

    TODO(SNOW-786577): Better data modeling for deployment interface."""

    def __init__(self, session: Session, manager: DeploymentManager) -> None:
        """Initializer of the Deployer.

        Args:
            session: The session used to connect to Snowflake.
            manager: The manager used to store the deployment information.
        """
        self._manager = manager
        self._session = session

    @overload
    def create_deployment(
        self,
        *,
        name: str,
        model_dir_path: str,
        platform: TargetPlatform,
        target_method: str,
        options: Optional[model_types.DeployOptions],
    ) -> Optional[Deployment]:
        """Create a deployment from a model in a local directory and deploy it to remote platform.

        Args:
            name: Name of the deployment for the model.
            platform: Target platform to deploy the model.
            target_method: The name of the target method to be deployed.
            model_dir_path: Directory of the model.
            options: Additional options when deploying the model.
                Each target platform will have their own specifications of options.
        """
        ...

    @overload
    def create_deployment(
        self,
        *,
        name: str,
        platform: TargetPlatform,
        target_method: str,
        model_stage_file_path: str,
        options: Optional[model_types.DeployOptions],
    ) -> Optional[Deployment]:
        """Create a deployment from a model in a zip file in a stage and deploy it to remote platform.

        Args:
            name: Name of the deployment for the model.
            platform: Target platform to deploy the model.
            target_method: The name of the target method to be deployed.
            model_stage_file_path: Model file in the stage to be deployed. Must be a file with .zip extension.
            options: Additional options when deploying the model.
                Each target platform will have their own specifications of options.
        """
        ...

    def create_deployment(
        self,
        *,
        name: str,
        platform: TargetPlatform,
        target_method: str,
        model_dir_path: Optional[str] = None,
        model_stage_file_path: Optional[str] = None,
        options: Optional[model_types.DeployOptions],
    ) -> Optional[Deployment]:
        """Create a deployment from a model and deploy it to remote platform.

        Args:
            name: Name of the deployment for the model.
            platform: Target platform to deploy the model.
            target_method: The name of the target method to be deployed.
            model_dir_path: Directory of the model. Exclusive with `model_stage_dir_path`.
            model_stage_file_path: Model file in the stage to be deployed. Exclusive with `model_dir_path`.
                Must be a file with .zip extension.
            options: Additional options when deploying the model.
                Each target platform will have their own specifications of options.

        Raises:
            RuntimeError: Raised when running into issues when deploying.
            ValueError: Raised when target method does not exist in model.

        Returns:
            The deployment information.
        """
        if not ((model_stage_file_path is None) ^ (model_dir_path is None)):
            raise ValueError(
                "model_dir_path and model_stage_file_path both cannot be "
                + f"{'None' if model_stage_file_path is None else 'specified'} at the same time."
            )

        is_success = False
        error_msg = ""
        info = None

        if not options:
            options = {}

        try:
            if platform == TargetPlatform.WAREHOUSE:
                meta = _udf_util._deploy_to_warehouse(
                    self._session,
                    model_dir_path=model_dir_path,
                    model_stage_file_path=model_stage_file_path,
                    udf_name=name,
                    target_method=target_method,
                    **options,
                )
            else:
                raise ValueError("Unsupported target Platform.")
            signature = meta.signatures.get(target_method, None)
            if not signature:
                raise ValueError(f"Target method {target_method} does not exist in model.")
            info = self._manager.create(name=name, platform=platform, signature=signature, options=options)
            is_success = True
        except Exception as e:
            print(e)
            error_msg = str(e)
        finally:
            if not is_success:
                if self._manager.get(name) is not None:
                    self._manager.delete(name)
                raise RuntimeError(error_msg)
        return info

    def list_deployments(self) -> List[Deployment]:
        """List all deployments in related deployment manager.

        Returns:
            A list of stored deployments information.
        """
        return self._manager.list()

    def get_deployment(self, name: str) -> Optional[Deployment]:
        """Get a specific deployment with the given name if exists in the related deployment manager.

        Args:
            name: Name of deployment.

        Returns:
            The deployment information. Return None if the requested deployment does not exist.
        """
        return self._manager.get(name)

    def delete_deployment(self, name: str) -> None:
        """Delete a deployment with the given name in the related deployment manager.

        Args:
            name: Name of deployment.
        """
        self._manager.delete(name)

    @overload
    def predict(self, name: str, X: model_types.SupportedLocalDataType) -> pd.DataFrame:
        """Execute batch inference of a model remotely on local data. Can be any supported data type. Return a local
            Pandas Dataframe.

        Args:
            name: The name of the deployment that contains the model used to infer.
            X: The input data.
        """
        ...

    @overload
    def predict(self, name: str, X: SnowparkDataFrame) -> SnowparkDataFrame:
        """Execute batch inference of a model remotely on a Snowpark DataFrame. Return a Snowpark DataFrame.

        Args:
            name: The name of the deployment that contains the model used to infer.
            X: The input Snowpark dataframe.

        """

    def predict(
        self, name: str, X: Union[model_types.SupportedDataType, SnowparkDataFrame]
    ) -> Union[pd.DataFrame, SnowparkDataFrame]:
        """Execute batch inference of a model remotely.

        Args:
            name: The name of the deployment that contains the model used to infer.
            X: The input dataframe.

        Raises:
            ValueError: Raised when the deployment does not exist.
            ValueError: Raised when the input is too large to use keep_order option.
            NotImplementedError: FeatureGroupSpec is not supported.

        Returns:
            The output dataframe.
        """
        # Initialize inference
        d = self.get_deployment(name)
        if not d:
            raise ValueError(f"Deployment {name} does not exist.")

        # Get options
        INTERMEDIATE_OBJ_NAME = "tmp_result"
        sig = d["signature"]
        keep_order = d["options"].get("keep_order", True)
        output_with_input_features = d["options"].get("output_with_input_features", False)

        # Validate and prepare input
        if not isinstance(X, SnowparkDataFrame):
            df = model_signature._convert_and_validate_local_data(X, sig.inputs)
            s_df = self._session.create_dataframe(df)
        else:
            model_signature._validate_snowpark_data(X, sig.inputs)
            s_df = X

        if keep_order:
            # ID is UINT64 type, this we should limit.
            if s_df.count() > 2**64:
                raise ValueError("Unable to keep order of a DataFrame with more than 2 ** 64 rows.")
            s_df = s_df.with_column(_udf_util._KEEP_ORDER_COL_NAME, F.monotonically_increasing_id())

        # Infer and get intermediate result
        input_cols = []
        for col_name in s_df.columns:
            literal_col_name = identifier.get_unescaped_names(col_name)
            input_cols.extend(
                [
                    type_utils.ColumnOrName(F.lit(type_utils.LiteralType(literal_col_name))),
                    type_utils.ColumnOrName(F.col(col_name)),
                ]
            )
        output_obj = F.call_udf(name, type_utils.ColumnOrLiteral(F.object_construct(*input_cols)))
        if output_with_input_features:
            df_res = s_df.with_column(INTERMEDIATE_OBJ_NAME, output_obj)
        else:
            df_res = s_df.select(output_obj.alias(INTERMEDIATE_OBJ_NAME))

        if keep_order:
            df_res = df_res.order_by(F.col(INTERMEDIATE_OBJ_NAME)[_udf_util._KEEP_ORDER_COL_NAME], ascending=True)
            if output_with_input_features:
                df_res = df_res.drop(_udf_util._KEEP_ORDER_COL_NAME)

        # Prepare the output
        output_cols = []
        for output_feature in sig.outputs:
            output_cols.append(
                F.col(INTERMEDIATE_OBJ_NAME)[output_feature.name].astype(output_feature.as_snowpark_type())
            )

        df_res = df_res.with_columns(
            [identifier.quote_name_without_upper_casing(output_feature.name) for output_feature in sig.outputs],
            output_cols,
        ).drop(INTERMEDIATE_OBJ_NAME)

        # Get final result
        if not isinstance(X, SnowparkDataFrame):
            dtype_map = {}
            for feature in sig.outputs:
                if isinstance(feature, model_signature.FeatureGroupSpec):
                    raise NotImplementedError("FeatureGroupSpec is not supported.")
                assert isinstance(feature, model_signature.FeatureSpec), "Invalid feature kind."
                dtype_map[feature.name] = feature.as_dtype()
            df_local = df_res.to_pandas()
            # This is because Array and object will generate variant type and requires an additional loads to
            # get correct data otherwise it would be string.
            for col_name in [col_name for col_name, col_dtype in dtype_map.items() if col_dtype == np.object0]:
                df_local[col_name] = df_local[col_name].map(json.loads)
            df_local = df_local.astype(dtype=dtype_map)
            return pd.DataFrame(df_local)
        else:
            return df_res