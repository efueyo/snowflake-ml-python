import inspect
from typing import Any, List, Optional

import numpy as np
import pandas as pd

from snowflake.ml._internal.exceptions import error_codes, exceptions


class PandasTransformHandlers:
    """Transform(inference and scoring) functions for a pandas dataset."""

    def __init__(
        self,
        dataset: pd.DataFrame,
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
        """
        self.dataset = dataset
        self.estimator = estimator
        self.class_name = class_name
        self.subproject = subproject
        self.autogenerated = autogenerated

    def batch_inference(
        self,
        inference_method: str,
        input_cols: List[str],
        expected_output_cols: List[str],
        snowpark_input_cols: Optional[List[str]] = None,
        drop_input_cols: Optional[bool] = False,
        *args: Any,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Run batch inference on the given dataset.

         Args:
            inference_method: the name of the method used by `estimator` to run inference.
            input_cols: column names of the input dataset
            expected_output_cols: column names (in order) of the output dataset.
            snowpark_input_cols: list of snowpark columns.
                Covers the situation where training happens in snowpark, transform in pandas.
            drop_input_cols: If set True, the response will not contain input columns.
            args: additional positional arguments.
            kwargs: additional keyword args.

        Returns:
            A new dataset of the same type as the input dataset.

        Raises:
            SnowflakeMLException: Mismatches between expected feature names and provided feature names.
            SnowflakeMLException: expected_output_cols list length does not match required length.
        """

        output_cols = expected_output_cols.copy()
        dataset = self.dataset
        # Model expects exact same columns names in the input df for predict call.
        # Given the scenario that user use snowpark DataFrame in fit call, but pandas DataFrame in predict call
        # input cols need to match unquoted / quoted

        if snowpark_input_cols is None:
            snowpark_input_cols = []

        if hasattr(self.estimator, "feature_names_in_"):
            features_required_by_estimator = self.estimator.feature_names_in_
        else:
            features_required_by_estimator = snowpark_input_cols

        missing_features = []
        features_in_dataset = set(dataset.columns)

        columns_to_select = []

        for i, f in enumerate(features_required_by_estimator):
            if (
                i >= len(input_cols)
                or (input_cols[i] != f and snowpark_input_cols[i] != f)
                or (input_cols[i] not in features_in_dataset and snowpark_input_cols[i] not in features_in_dataset)
            ):
                missing_features.append(f)
            elif input_cols[i] in features_in_dataset:
                columns_to_select.append(input_cols[i])
            elif snowpark_input_cols[i] in features_in_dataset:
                columns_to_select.append(snowpark_input_cols[i])

        if len(missing_features) > 0:
            raise exceptions.SnowflakeMLException(
                error_code=error_codes.NOT_FOUND,
                original_exception=ValueError(
                    "The feature names should match with those that were passed during fit.\n"
                    f"Features seen during fit call but not present in the input: {missing_features}\n"
                    f"Features in the input dataframe : {input_cols}\n"
                ),
            )
        input_df = dataset[columns_to_select]
        input_df.columns = features_required_by_estimator

        inference_res = getattr(self.estimator, inference_method)(input_df, *args, **kwargs)

        if isinstance(inference_res, list) and len(inference_res) > 0 and isinstance(inference_res[0], np.ndarray):
            # In case of multioutput estimators, predict_proba, decision_function etc., functions return a list of
            # ndarrays. We need to concatenate them.

            # First compute output column names
            if len(output_cols) == len(inference_res):
                actual_output_cols = []
                for idx, np_arr in enumerate(inference_res):
                    for i in range(1 if len(np_arr.shape) <= 1 else np_arr.shape[1]):
                        actual_output_cols.append(f"{output_cols[idx]}_{i}")
                output_cols = actual_output_cols

            # Concatenate np arrays
            transformed_numpy_array = np.concatenate(inference_res, axis=1)
        elif isinstance(inference_res, tuple) and len(inference_res) > 0 and isinstance(inference_res[0], np.ndarray):
            # In case of kneighbors, functions return a tuple of ndarrays.
            transformed_numpy_array = np.stack(inference_res, axis=1)
        else:
            transformed_numpy_array = inference_res

        if (len(transformed_numpy_array.shape) == 3) and inference_method != "kneighbors":
            # VotingClassifier will return results of shape (n_classifiers, n_samples, n_classes)
            # when voting = "soft" and flatten_transform = False. We can't handle unflatten transforms,
            # so we ignore flatten_transform flag and flatten the results.
            transformed_numpy_array = np.hstack(transformed_numpy_array)  # type: ignore[call-overload]

        if len(transformed_numpy_array.shape) == 1:
            transformed_numpy_array = np.reshape(transformed_numpy_array, (-1, 1))

        shape = transformed_numpy_array.shape
        if shape[1] != len(output_cols):
            if len(output_cols) != 1:
                raise exceptions.SnowflakeMLException(
                    error_code=error_codes.INVALID_ARGUMENT,
                    original_exception=TypeError(
                        "expected_output_cols must be same length as transformed array or " "should be of length 1"
                    ),
                )
            actual_output_cols = []
            for i in range(shape[1]):
                actual_output_cols.append(f"{output_cols[0]}_{i}")
            output_cols = actual_output_cols

        if inference_method == "kneighbors":
            if len(transformed_numpy_array.shape) == 3:  # return_distance=True
                shape = transformed_numpy_array.shape
                data = [transformed_numpy_array[:, i, :].tolist() for i in range(shape[1])]
                kneighbors_df = pd.DataFrame({output_cols[i]: data[i] for i in range(shape[1])})
            else:  # return_distance=False
                kneighbors_df = pd.DataFrame(
                    {
                        {
                            output_cols[0]: [
                                transformed_numpy_array[i, :].tolist() for i in range(transformed_numpy_array.shape[0])
                            ]
                        }
                    }
                )

            if drop_input_cols:
                dataset = kneighbors_df
            else:
                dataset = pd.concat([dataset, kneighbors_df], axis=1)
        else:
            if drop_input_cols:
                dataset = pd.DataFrame(data=transformed_numpy_array, columns=output_cols)
            else:
                dataset = dataset.copy()
                dataset[output_cols] = transformed_numpy_array
        return dataset

    def score(
        self,
        input_cols: List[str],
        label_cols: List[str],
        sample_weight_col: Optional[str],
        *args: Any,
        **kwargs: Any,
    ) -> float:
        """Score the given test dataset.

        Args:
            input_cols: List of feature columns for scoring.
            label_cols: List of label columns for scoring.
            sample_weight_col: A column assigning relative weights to each row for scoring.
            args: additional positional arguments.
            kwargs: additional keyword args.

        Returns:
             An accuracy score for the model on the given test data.

        Raises:
            SnowflakeMLException: The input column list does not have one of `X` and `X_test`.
        """
        assert hasattr(self.estimator, "score")  # make type checker happy
        argspec = inspect.getfullargspec(self.estimator.score)
        if "X" in argspec.args:
            score_args = {"X": self.dataset[input_cols]}
        elif "X_test" in argspec.args:
            score_args = {"X_test": self.dataset[input_cols]}
        else:
            raise exceptions.SnowflakeMLException(
                error_code=error_codes.INVALID_ATTRIBUTE,
                original_exception=RuntimeError("Neither 'X' or 'X_test' exist in argument"),
            )

        if len(label_cols) > 0:
            label_arg_name = "Y" if "Y" in argspec.args else "y"
            score_args[label_arg_name] = self.dataset[label_cols].squeeze()

        if sample_weight_col is not None and "sample_weight" in argspec.args:
            score_args["sample_weight"] = self.dataset[sample_weight_col].squeeze()

        score = self.estimator.score(**score_args)
        assert isinstance(score, float)  # make type checker happy

        return score