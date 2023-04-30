import functools
import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

import anyio
import pandas as pd

from snowflake.ml.model import model_types


class MethodRef:
    """Represents an method invocation of an instance of `ModelRef`.

    This allows us to
        1) Customize the place of actual execution of the method(inline, thread/process pool or remote).
        2) Enrich the way of execution(sync versus async).
    Example:
        If you have a SKL model, you would normally invoke by `skl_ref.predict(df)` which has sync API.
        Within inference graph, you could invoke `await skl_ref.predict.async_run(df)` which automatically
        will be run on thread with async interface.
    """

    def __init__(self, model_ref: "ModelRef", method_name: str) -> None:
        self._func = getattr(model_ref.model, method_name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._func(*args, **kwargs)

    async def async_run(self, *args: Any, **kwargs: Any) -> Any:
        """Run the method in a async way. If the method is defined as async, this will simply run it. If not, this will
        be run in a separate thread.

        Args:
            *args: Arguments of the original method,
            **kwargs: Keyword arguments of the original method.

        Returns:
            The result of the original method.
        """
        if inspect.iscoroutinefunction(self._func):
            return await self._func(*args, **kwargs)
        return await anyio.to_thread.run_sync(functools.partial(self._func, **kwargs), *args)


class ModelRef:
    """Represents an model in the inference graph. Method could be directly called using this reference object as if
    with the original model object.

    This enables us to separate physical and logical representation of a model which
    will allows us to deeply understand the graph and perform optimization at entire
    graph level.
    """

    def __init__(self, name: str, model: model_types.ModelType) -> None:
        """Initialize the ModelRef.

        Args:
            name: The name of a model to refer it.
            model: The model object.
        """
        self._model = model
        self._name = name

    @property
    def name(self) -> str:
        """The name of the sub-model."""
        return self._name

    @property
    def model(self) -> model_types.ModelType:
        """The model object of the sub-model."""
        return self._model

    def __getattr__(self, method_name: str) -> Any:
        if hasattr(self._model, method_name):
            return MethodRef(self, method_name)
        raise TypeError(f"Model is does not have {method_name}.")

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        del state["_model"]
        return state

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        if callable(self._model):
            return MethodRef(self, "__call__")(*args, **kwds)
        raise TypeError("Model is not callable.")

    def __setstate__(self, state: Any) -> None:
        self.__dict__.update(state)


class ModelContext:
    """Context for a custom model showing path to artifacts and mapping between model name and object reference.

    Attributes:
        artifacts: A dict mapping name of the artifact to its path.
        model_refs: A dict mapping name of the sub-model to its ModelRef object.
    """

    def __init__(
        self,
        *,
        artifacts: Optional[Dict[str, str]] = None,
        models: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize the model context

        Args:
            artifacts: A dict mapping name of the artifact to its currently available path. Defaults to None.
            models: A dict mapping name of the sub-model to the corresponding model object. Defaults to None.
        """
        self.artifacts = artifacts if artifacts else dict()
        self.model_refs = {name: ModelRef(name, model) for name, model in models.items()} if models else dict()

    def path(self, key: str) -> str:
        """Get the actual path to a specific artifact.

        Args:
            key: The name of the artifact.

        Returns:
            The absolute path to the artifact.
        """
        return self.artifacts[key]

    def model_ref(self, name: str) -> ModelRef:
        """Get a ModelRef object of a sub-model containing the name and model object, while able to call its method
        directly as well.

        Args:
            name: The name of the sub-model.

        Returns:
            The ModelRef object to the sub-model.
        """
        return self.model_refs[name]


def _validate_predict_function(func: Callable[["CustomModel", pd.DataFrame], pd.DataFrame]) -> None:
    """Validate the user provided predict method.

    Args:
        func: The predict method.

    Raises:
        TypeError: Raised when the method is not a callable object.
        TypeError: Raised when the method does not have 2 arguments (self and X).
        TypeError: Raised when the method does not have typing annotation.
        TypeError: Raised when the method's input (X) does not have type pd.DataFrame.
        TypeError: Raised when the method's output does not have type pd.DataFrame.
    """
    if not callable(func):
        raise TypeError("Predict method is not callable.")

    func_signature = inspect.signature(func)
    if len(func_signature.parameters) != 2:
        raise TypeError("Predict method should have exact 2 arguments.")

    input_annotation = list(func_signature.parameters.values())[1].annotation
    output_annotation = func_signature.return_annotation

    if input_annotation == inspect.Parameter.empty or output_annotation == inspect.Signature.empty:
        raise TypeError("Missing type annotation for predict method.")

    if input_annotation != pd.core.frame.DataFrame:
        raise TypeError("Input for predict method should have type pandas.DataFrame.")

    if output_annotation != pd.core.frame.DataFrame:
        raise TypeError("Output for predict method should have type pandas.DataFrame.")


class CustomModel(ABC):
    """Abstract class for user defined custom model.

    Attributes:
        context: A ModelContext object showing sub-models and artifacts related to this model.
    """

    def __init__(self, context: ModelContext) -> None:
        self.context = context
        _validate_predict_function(type(self).predict)

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Predict method for model inference.

        Args:
            X: The input dataframe.
        """
        ...

    def __setattr__(self, __name: str, __value: Any) -> None:
        # A hook for case when users reassign the predict method.
        if __name == "predict":
            _validate_predict_function(__value)
        return super().__setattr__(__name, __value)