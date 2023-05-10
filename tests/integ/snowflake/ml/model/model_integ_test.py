#
# Copyright (c) 2012-2022 Snowflake Computing Inc. All rights reserved.
#

import asyncio
import os
import random
import string
import sys
import tempfile

import numpy as np
import pandas as pd
from absl import flags
from absl.testing import absltest
from packaging import utils as packaging_utils
from sklearn import datasets, ensemble, linear_model, multioutput

from snowflake.ml.model import (
    _deployer,
    _model as model_api,
    custom_model,
    type_hints as model_types,
)
from snowflake.ml.utils import connection_params
from snowflake.snowpark import Session

flags.FLAGS(sys.argv)


class DemoModel(custom_model.CustomModel):
    def __init__(self, context: custom_model.ModelContext) -> None:
        super().__init__(context)

    @custom_model.inference_api
    def predict(self, input: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"output": input["c1"]})


class AsyncComposeModel(custom_model.CustomModel):
    def __init__(self, context: custom_model.ModelContext) -> None:
        super().__init__(context)

    @custom_model.inference_api
    async def predict(self, input: pd.DataFrame) -> pd.DataFrame:
        res1 = await self.context.model_ref("m1").predict.async_run(input)
        res_sum = res1["output"] + self.context.model_ref("m2").predict(input)["output"]
        return pd.DataFrame({"output": res_sum / 2})


class DemoModelWithArtifacts(custom_model.CustomModel):
    def __init__(self, context: custom_model.ModelContext) -> None:
        super().__init__(context)
        with open(context.path("bias")) as f:
            v = int(f.read())
        self.bias = v

    @custom_model.inference_api
    def predict(self, input: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"output": (input["c1"] + self.bias) > 12})


def _upload_snowml_to_tmp_stage(
    session: Session,
) -> str:
    """Upload model module of snowml to tmp stage.

    Args:
        session: Snowpark session.

    Returns:
        The stage path to uploaded snowml.zip file.
    """
    root_paths = [
        os.path.join(absltest.TEST_SRCDIR.value, "SnowML", "snowflake", "ml"),  # Test using bazel
        os.path.join(absltest.TEST_SRCDIR.value, "bazel-bin", "snowflake", "ml"),  # Test using pytest
        os.path.join(absltest.TEST_SRCDIR.value),  # Test in Jenkins Wheel build and test pipeline.
    ]
    whl_filename = None
    for root_path in root_paths:
        if not os.path.exists(root_path):
            continue
        for filename in os.listdir(root_path):
            if os.path.splitext(filename)[-1] == ".whl":
                try:
                    packaging_utils.parse_wheel_filename(filename=filename)
                    whl_filename = filename
                    break
                except packaging_utils.InvalidWheelFilename:
                    continue
        if whl_filename:
            break
    if whl_filename is None:
        raise RuntimeError("Cannot file wheel file. Have it been built?")
    whl_path = os.path.join(root_path, whl_filename)
    tmp_stage = session.get_session_stage()
    _ = session.file.put(whl_path, tmp_stage, auto_compress=False, overwrite=True)
    return f"{tmp_stage}/{whl_filename}"


class TestModelInteg(absltest.TestCase):
    @classmethod
    def setUpClass(self) -> None:
        """Creates Snowpark and Snowflake environments for testing."""
        self._session = Session.builder.configs(connection_params.SnowflakeLoginOptions()).create()
        # To create different UDF names among different runs
        self.run_id = "".join(random.choices(string.ascii_lowercase, k=8))
        self._snowml_wheel_path = _upload_snowml_to_tmp_stage(self._session)

    @classmethod
    def tearDownClass(self) -> None:
        self._session.close()

    def test_async_model_composition(self) -> None:
        async def _test(self: "TestModelInteg") -> None:
            arr = np.random.randint(100, size=(10000, 3))
            d = pd.DataFrame(arr, columns=["c1", "c2", "c3"])
            clf = DemoModel(custom_model.ModelContext())
            model_context = custom_model.ModelContext(
                models={
                    "m1": clf,
                    "m2": clf,
                }
            )
            acm = AsyncComposeModel(model_context)
            with tempfile.TemporaryDirectory() as tmpdir:
                model_api.save_model(
                    name="async_model_composition",
                    model_dir_path=os.path.join(tmpdir, "async_model_composition"),
                    model=acm,
                    sample_input=d,
                    metadata={"author": "halu", "version": "1"},
                )
                dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
                di = dc.create_deployment(
                    name=f"async_model_composition_{self.run_id}",
                    model_dir_path=os.path.join(tmpdir, "async_model_composition"),
                    platform=_deployer.TargetPlatform.WAREHOUSE,
                    target_method="predict",
                    options=model_types.WarehouseDeployOptions(
                        {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                    ),
                )

                assert di is not None
                res = dc.predict(di["name"], d)

                pd.testing.assert_frame_equal(
                    res,
                    pd.DataFrame(arr[:, 0], columns=["output"], dtype=float),
                )

                self.assertTrue(di in dc.list_deployments())
                self.assertEqual(di, dc.get_deployment(f"async_model_composition_{self.run_id}"))

        asyncio.get_event_loop().run_until_complete(_test(self))

    def test_bad_model_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lm = DemoModel(custom_model.ModelContext())
            arr = np.array([[1, 2, 3], [4, 2, 5]])
            d = pd.DataFrame(arr, columns=["c1", "c2", "c3"])
            model_api.save_model(
                name="custom_bad_model",
                model_dir_path=os.path.join(tmpdir, "custom_bad_model"),
                model=lm,
                sample_input=d,
                metadata={"author": "halu", "version": "1"},
                conda_dependencies=["numpy==1.22.4"],
            )

            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            with self.assertRaises(RuntimeError):
                _ = dc.create_deployment(
                    name=f"custom_bad_model_{self.run_id}",
                    model_dir_path=os.path.join(tmpdir, "custom_bad_model"),
                    platform=_deployer.TargetPlatform.WAREHOUSE,
                    target_method="predict",
                    options=model_types.WarehouseDeployOptions(
                        {"relax_version": False, "_snowml_wheel_path": self._snowml_wheel_path}
                    ),
                )

            with self.assertRaises(ValueError):
                _ = dc.predict(f"custom_bad_model_{self.run_id}", d)

    def test_custom_demo_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lm = DemoModel(custom_model.ModelContext())
            arr = np.random.randint(100, size=(10000, 3))
            d = pd.DataFrame(arr, columns=["c1", "c2", "c3"])
            model_api.save_model(
                name="custom_demo_model",
                model_dir_path=os.path.join(tmpdir, "custom_demo_model"),
                model=lm,
                sample_input=d,
                metadata={"author": "halu", "version": "1"},
            )

            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            di = dc.create_deployment(
                name=f"custom_demo_model_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "custom_demo_model", ""),  # Test sanitizing user path input.
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di is not None
            res = dc.predict(di["name"], d)

            pd.testing.assert_frame_equal(
                res,
                pd.DataFrame(arr[:, 0], columns=["output"]),
            )

            self.assertTrue(di in dc.list_deployments())
            self.assertEqual(di, dc.get_deployment(f"custom_demo_model_{self.run_id}"))

    def test_custom_model_with_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "bias"), "w") as f:
                f.write("10")
            lm = DemoModelWithArtifacts(
                custom_model.ModelContext(models={}, artifacts={"bias": os.path.join(tmpdir, "bias")})
            )
            arr = np.array([[1, 2, 3], [4, 2, 5]])
            d = pd.DataFrame(arr, columns=["c1", "c2", "c3"])
            model_api.save_model(
                name="custom_model_with_artifacts",
                model_dir_path=os.path.join(tmpdir, "custom_model_with_artifacts"),
                model=lm,
                sample_input=d,
                metadata={"author": "halu", "version": "1"},
            )

            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            di = dc.create_deployment(
                name=f"custom_model_with_artifacts_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "custom_model_with_artifacts"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di is not None
            res = dc.predict(di["name"], d)

        pd.testing.assert_frame_equal(
            res,
            pd.DataFrame([False, True], columns=["output"]),
        )

    def test_skl_model_deploy(self) -> None:
        iris_X, iris_y = datasets.load_iris(return_X_y=True)
        regr = linear_model.LinearRegression()
        regr.fit(iris_X[:10], iris_y[:10])
        with tempfile.TemporaryDirectory() as tmpdir:
            model_api.save_model(
                name="skl_model",
                model_dir_path=os.path.join(tmpdir, "skl_model"),
                model=regr,
                sample_input=iris_X,
                metadata={"author": "halu", "version": "1"},
            )
            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            di = dc.create_deployment(
                name=f"skl_model_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "skl_model"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )

            assert di is not None
            res = dc.predict(di["name"], iris_X)
            np.testing.assert_allclose(res["feature_0"].values, regr.predict(iris_X))

    def test_skl_model_proba_deploy(self) -> None:
        iris_X, iris_y = datasets.load_iris(return_X_y=True)
        model = ensemble.RandomForestClassifier(random_state=42)
        model.fit(iris_X[:10], iris_y[:10])
        with tempfile.TemporaryDirectory() as tmpdir:
            model_api.save_model(
                name="skl_model_proba",
                model_dir_path=os.path.join(tmpdir, "skl_model_proba"),
                model=model,
                sample_input=iris_X,
                metadata={"author": "halu", "version": "1"},
                conda_dependencies=["scikit-learn"],
            )
            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            di_predict = dc.create_deployment(
                name=f"skl_model_predict_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "skl_model_proba"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di_predict is not None
            res = dc.predict(di_predict["name"], iris_X[:10])
            np.testing.assert_allclose(res["feature_0"].values, model.predict(iris_X[:10]))

            di_predict_proba = dc.create_deployment(
                name=f"skl_model_predict_proba_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "skl_model_proba"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict_proba",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di_predict_proba is not None
            res = dc.predict(di_predict_proba["name"], iris_X[:10])
            np.testing.assert_allclose(res.values, model.predict_proba(iris_X[:10]))

    def test_skl_multiple_output_model_proba_deploy(self) -> None:
        iris_X, iris_y = datasets.load_iris(return_X_y=True)
        target2 = np.random.randint(0, 6, size=iris_y.shape)
        dual_target = np.vstack([iris_y, target2]).T
        model = multioutput.MultiOutputClassifier(ensemble.RandomForestClassifier(random_state=42))
        model.fit(iris_X[:10], dual_target[:10])
        with tempfile.TemporaryDirectory() as tmpdir:
            model_api.save_model(
                name="skl_multiple_output_model_proba",
                model_dir_path=os.path.join(tmpdir, "skl_multiple_output_model_proba"),
                model=model,
                sample_input=iris_X,
                metadata={"author": "halu", "version": "1"},
                conda_dependencies=["scikit-learn"],
            )
            dc = _deployer.Deployer(self._session, _deployer.LocalDeploymentManager())
            di_predict = dc.create_deployment(
                name=f"skl_multiple_output_model_predict_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "skl_multiple_output_model_proba"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di_predict is not None
            res = dc.predict(di_predict["name"], iris_X[-10:])
            np.testing.assert_allclose(res.values, model.predict(iris_X[-10:]))

            di_predict_proba = dc.create_deployment(
                name=f"skl_multiple_output_model_predict_proba_{self.run_id}",
                model_dir_path=os.path.join(tmpdir, "skl_multiple_output_model_proba"),
                platform=_deployer.TargetPlatform.WAREHOUSE,
                target_method="predict_proba",
                options=model_types.WarehouseDeployOptions(
                    {"relax_version": True, "_snowml_wheel_path": self._snowml_wheel_path}
                ),
            )
            assert di_predict_proba is not None
            res = dc.predict(di_predict_proba["name"], iris_X[-10:])
            np.testing.assert_allclose(res.values, np.hstack(model.predict_proba(iris_X[-10:])))


if __name__ == "__main__":
    absltest.main()
