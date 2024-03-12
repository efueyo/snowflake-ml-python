from typing import Any, List, Optional

from snowflake.snowpark import DataFrame, Session


class MLRuntimeTransformHandlers:
    def __init__(
        self,
        dataset: DataFrame,
        estimator: object,
        class_name: str,
        subproject: str,
        autogenerated: Optional[bool] = False,
    ) -> None:
        """
        Args:
            dataset: The dataset to run transform functions on.
            estimator: The estimator used to run transforms.
            class_name: class name to be used in telemetry.
            subproject: subproject to be used in telemetry.
            autogenerated: Whether the class was autogenerated from a template.

        Raises:
            ModuleNotFoundError: The mlruntimes_client module is not available.
        """
        try:
            from snowflake.ml.runtime import MLRuntimeClient
        except ModuleNotFoundError as e:
            # This is an internal exception, not a user-facing one. The snowflake.ml.runtime module should
            # always be present when this class is instantiated.
            raise e

        self.client = MLRuntimeClient()
        self.dataset = dataset
        self.estimator = estimator
        self._class_name = class_name
        self._subproject = subproject
        self._autogenerated = autogenerated

    def batch_inference(
        self,
        inference_method: str,
        input_cols: List[str],
        expected_output_cols: List[str],
        pass_through_cols: List[str],
        session: Session,
        dependencies: List[str],
        expected_output_cols_type: Optional[str] = "",
        *args: Any,
        **kwargs: Any,
    ) -> DataFrame:
        """Run batch inference on the given dataset.

        Args:
            inference_method: the name of the method used by `estimator` to run inference.
            input_cols: List of feature columns for inference.
            session: An active Snowpark Session.
            dependencies: List of dependencies for the transformer.
            expected_output_cols: column names (in order) of the output dataset.
            pass_through_cols: columns in the dataset not used in inference.
            expected_output_cols_type: Expected type of the output columns.
            args: additional positional arguments.
            kwargs: additional keyword args.

        Returns:
            A new dataset of the same type as the input dataset.

        Raises:
            TypeError: The ML Runtimes client returned a non-DataFrame result.
        """
        output_df = self.client.batch_inference(
            inference_method=inference_method,
            dataset=self.dataset,
            estimator=self.estimator,
            input_cols=input_cols,
            expected_output_cols=expected_output_cols,
            pass_through_cols=pass_through_cols,
            session=session,
            dependencies=dependencies,
            expected_output_cols_type=expected_output_cols_type,
            *args,
            **kwargs,
        )
        if not isinstance(output_df, DataFrame):
            raise TypeError(
                f"The ML Runtimes Client did not return a DataFrame a non-float value Returned type: {type(output_df)}"
            )
        return output_df

    def score(
        self,
        input_cols: List[str],
        label_cols: List[str],
        session: Session,
        dependencies: List[str],
        score_sproc_imports: List[str],
        sample_weight_col: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> float:
        """Score the given test dataset.

        Args:
            session: An active Snowpark Session.
            dependencies: score function dependencies.
            score_sproc_imports: imports for score stored procedure.
            input_cols: List of feature columns for inference.
            label_cols: List of label columns for scoring.
            sample_weight_col: A column assigning relative weights to each row for scoring.
            args: additional positional arguments.
            kwargs: additional keyword args.


        Returns:
            An accuracy score for the model on the given test data.

        Raises:
            TypeError: The ML Runtimes client returned a non-float result
        """
        output_score = self.client.score(
            estimator=self.estimator,
            dataset=self.dataset,
            input_cols=input_cols,
            label_cols=label_cols,
            sample_weight_col=sample_weight_col,
        )
        if not isinstance(output_score, float):
            raise TypeError(
                f"The ML Runtimes Client returned a non-float value {output_score} of type {type(output_score)}"
            )
        return output_score