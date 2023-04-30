import functools
import inspect
import logging
from typing import Any, Callable, List, Optional

from snowflake import snowpark
from snowflake.connector import connection
from snowflake.ml.fileset import fileset_errors, sfcfs
from snowflake.ml.utils import identifier, import_utils, telemetry
from snowflake.snowpark import exceptions, functions, types
from snowflake.snowpark._internal import type_utils as snowpark_types

# The max file size for data loading.
TARGET_FILE_SIZE = 32 * 2**20

# Expected type of a stage where a FileSet can be located.
# The type is the value of the 'type' column of a `show stages` query.
_FILESET_STAGE_TYPE = "INTERNAL NO CSE"

_PROJECT = "FileSet"


def _raise_if_deleted(func: Callable[..., Any]) -> Callable[..., Any]:
    """A function decorator where an error will be raised when the fileset has been deleted."""

    @functools.wraps(func)
    def raise_if_deleted_helper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self._is_deleted:
            raise fileset_errors.FileSetAlreadyDeletedError("The FileSet has already been deleted.")
        return func(self, *args, **kwargs)

    return raise_if_deleted_helper


class FileSet:
    """A FileSet represents an immutable snapshot of the result of a query in the form of files."""

    def __init__(
        self,
        *,
        target_stage_loc: str,
        name: str,
        sf_connection: Optional[connection.SnowflakeConnection] = None,
        snowpark_session: Optional[snowpark.Session] = None,
    ) -> None:
        """Create a FileSet based on an existing stage directory.

        It can be used to restore an existing FileSet that was not deleted before.

        Args:
            sf_connection: A Snowflake python connection object. Mutually exclusive to `snowpark_session`.
            snowpark_session: A Snowpark Session object. Mutually exclusive to `sf_connection`.
            target_stage_loc: A string of the Snowflake stage path where the FileSet will be stored.
                It needs to be an absolute path with the form of "@{database}.{schema}.{stage}/{optional directory}/".
            name: The name of the FileSet. It is the name of the directory which holds result stage files.

        Raises:
            ValueError: An error occured when not exactly one of sf_connection and snowpark_session is given.

        Example:
        >>> # Create a new FileSet using Snowflake Python connection
        >>> conn = snowflake.connector.connect(**connection_parameters)
        >>> my_fileset = snowflake.ml.fileset.FileSet.make(
        >>>     target_stage_loc="@mydb.mychema.mystage/mydir"
        >>>     name="helloworld",
        >>>     sf_connection=conn,
        >>>     query="SELECT * FROM Mytable limit 1000000",
        >>> )
        >>> my_fileset.files()
        ----
        ['sfc://@mydb.myschema.mystage//mydir/helloworld/data_0_0_0.snappy.parquet']

        >>> # Now we can restore the FileSet in another program as long as the FileSet is not deleted
        >>> conn = snowflake.connector.connect(**connection_parameters)
        >>> my_fileset_pointer = FileSet(sf_connection=conn,
                                         target_stage_loc="@mydb.mychema.mystage/mydir",
                                         name="helloworld")
        >>> my_fileset.files()
        ----
        ['sfc://@mydb.myschema.mystage/mydir/helloworld/data_0_0_0.snappy.parquet']
        """
        if sf_connection and snowpark_session:
            raise ValueError("sf_connection and snowpark_session cannot be specified at the same time.")
        if not sf_connection and not snowpark_session:
            raise ValueError("sf_connection or snowpark_session must be provided")
        self._snowpark_session = (
            snowpark_session
            if snowpark_session
            else snowpark.Session.builder.config("connection", sf_connection).create()
        )
        self._target_stage_loc = target_stage_loc
        _validate_target_stage_loc(self._snowpark_session, self._target_stage_loc)
        self._name = name
        # We want the whole file to be downloaded into memory upon the first head.
        # Because the actual file size might be larger TARGET_FILE_SIZE, we use a larger buffer ceiling.
        self._fs = sfcfs.SFFileSystem(
            snowpark_session=self._snowpark_session,
            cache_type="bytes",
            block_size=2 * TARGET_FILE_SIZE,
        )
        self._files: List[str] = []
        self._is_deleted = False

        _validate_files_from_same_query(self.files(), self._fileset_absolute_path())

    @classmethod
    def make(
        cls,
        *,
        target_stage_loc: str,
        name: str,
        snowpark_dataframe: Optional[snowpark.DataFrame] = None,
        sf_connection: Optional[connection.SnowflakeConnection] = None,
        query: str = "",
        shuffle: bool = False,
    ) -> "FileSet":
        """Creates a FileSet object given a SQL query.

        The result FileSet object captures the query result deterministically as stage files.

        Args:
            target_stage_loc: A string of the Snowflake stage path where the FileSet will be stored.
                It needs to be an absolute path with the form of "@{database}.{schema}.{stage}/{optional directory}/".
            name: The name of the FileSet. It will become the name of the directory which holds result stage files.
                If there is already a FileSet with the same name in the given stage location,
                an exception will be raised.
            snowpark_dataframe: A Snowpark Dataframe. Mutually exclusive to (`sf_connection`, `query`).
            sf_connection: A Snowflake python connection object. Must be provided if `query` is provided.
            query: A string of Snowflake SQL query to be executed. Mutually exclusive to `snowpark_dataframe`. Must
                also specify `sf_connection`.
            shuffle: A boolean represents whether the data should be shuffled globally. Default to be false.

        Returns:
            A FileSet object.

        Raises:
            ValueError: An error occured when not exactly one of sf_connection and snowpark_session is given.
            FileSetExistError: An error occured whern a FileSet with the same name exists in the given path.
            FileSetError: An error occured when the SQL query/dataframe is not able to get materialized.


        Example 1: Create a FileSet with Snowflake Python connection
        --------
        >>> conn = snowflake.connector.connect(**connection_parameters)
        >>> my_fileset = snowflake.ml.fileset.FileSet.make(
        >>>     target_stage_loc="@mydb.mychema.mystage/mydir"
        >>>     name="helloworld",
        >>>     sf_connection=conn,
        >>>     query="SELECT * FROM mytable limit 1000000",
        >>> )
        >>> my_fileset.files()
        ----
        ['sfc://@mydb.myschema.mystage/helloworld/data_0_0_0.snappy.parquet']

        Example 2: Create a FileSet with a Snowpark dataframe
        --------
        >>> new_session = snowflake.snowpark.Session.builder.configs(connection_parameters).create()
        >>> df = new_session.sql("SELECT * FROM Mytable limit 1000000")
        >>> my_fileset = snowflake.ml.fileset.FileSet.make(
        >>>     target_stage_loc="@mydb.mychema.mystage/mydir"
        >>>     name="helloworld",
        >>>     snowpark_dataframe=df,
        >>> )
        >>> my_fileset.files()
        ----
        ['sfc://@mydb.myschema.mystage/helloworld/data_0_0_0.snappy.parquet']
        """
        if snowpark_dataframe and sf_connection:
            raise ValueError("sf_connection and snowpark_session cannot be specified at the same time.")

        if not snowpark_dataframe:
            if not sf_connection:
                raise ValueError("Either snowpark_dataframe or sf_connection should be non-empty.")
            if not query:
                raise ValueError("Please use non-empty query to generate meaningful result.")
            snowpark_session = snowpark.Session.builder.config("connection", sf_connection).create()
            snowpark_dataframe = snowpark_session.sql(query)

        assert snowpark_dataframe is not None
        assert snowpark_dataframe._session is not None
        snowpark_session = snowpark_dataframe._session
        casted_df = _cast_snowpark_dataframe(snowpark_dataframe)

        _validate_target_stage_loc(snowpark_session, target_stage_loc)
        target_stage_exists = snowpark_session.sql(f"List {_fileset_absoluate_path(target_stage_loc, name)}").collect()
        if target_stage_exists:
            raise fileset_errors.FileSetExistError(f"FileSet with name {name} has already existed.")

        if shuffle:
            casted_df = casted_df.order_by(functions.random())

        try:
            # partition_by helps generate more uniform sharding among files.
            # As a side effect, the sizes of generate files might exceed max_file_size.
            # "partition_by=name" assigns the same sharding key <name> to all rows, resulting in all the generated files
            # located in <target_stage_loc>/<name>/ directory.
            # typing: snowpark's function signature is bogus.
            casted_df.write.copy_into_location(  # type: ignore
                location=target_stage_loc,
                file_format_type="parquet",
                header=True,
                partition_by=snowpark_types.ColumnOrSqlExpr(f"'{name}'"),
                max_file_size=TARGET_FILE_SIZE,
                detailed_output=True,
                statement_params=telemetry.get_function_usage_statement_params(
                    project=_PROJECT,
                    function_name=telemetry.get_statement_params_full_func_name(
                        inspect.currentframe(), cls.__class__.__name__
                    ),
                    api_calls=[snowpark.DataFrameWriter.copy_into_location],
                ),
            )
        except exceptions.SnowparkClientException as e:
            # Snowpark wraps the Python Connector error code in the head of the error message.
            if e.message.startswith(fileset_errors.ERRNO_FILE_EXIST_IN_STAGE):
                raise fileset_errors.FileSetExistError(f"FileSet with name {name} has already existed.")
            else:
                raise fileset_errors.FileSetError(str(e))

        return cls(target_stage_loc=target_stage_loc, name=name, snowpark_session=snowpark_session)

    @property
    def name(self) -> str:
        """Get the name of the FileSet."""
        return self._name

    def _fileset_absolute_path(self) -> str:
        """Get the Snowflake absoluate path to this FileSet directory."""
        return _fileset_absoluate_path(self._target_stage_loc, self.name)

    @telemetry.send_api_usage_telemetry(
        project=_PROJECT,
    )
    @_raise_if_deleted
    def files(self) -> List[str]:
        """Get the list of stage file paths in the current FileSet.

        The stage file paths follows the sfc protocol.

        Returns:
            A list of stage file paths

        Example:
        >>> my_fileset = FileSet(sf_connection=conn, target_stage_loc="@mydb.mychema.mystage", name="test")
        >>> my_fileset.files()
        ----
        ["sfc://@mydb.myschema.mystage/test/hello_world_0_0_0.snappy.parquet",
         "sfc://@mydb.myschema.mystage/test/hello_world_0_0_1.snappy.parquet"]
        """
        if self._files:
            return self._files
        loc = self._fileset_absolute_path()

        # TODO(zzhu)[SNOW-703491]: We could use manifest file to speed up file listing
        files = self._fs.ls(loc)
        self._files = [f"sfc://{file}" for file in files]
        return self._files

    @_raise_if_deleted
    def fileset_stage_location(self) -> str:
        """Get the stage path to the current FileSet in sfc protocol.

        Returns:
            A string representing the stage path

        Example:
        >>> my_fileset = FileSet(sf_connection=conn, target_stage_loc="@mydb.mychema.mystage", name="test")
        >>> my_fileset.files()
        ----
        "sfc://@mydb.myschema.mystage/test/
        """
        location = self._fileset_absolute_path()
        location = "sfc://" + location
        return location

    @telemetry.send_api_usage_telemetry(
        project=_PROJECT,
        func_params_to_log=["batch_size", "shuffle", "drop_last_batch"],
    )
    @_raise_if_deleted
    def to_torch_datapipe(self, *, batch_size: int, shuffle: bool = False, drop_last_batch: bool = True) -> Any:
        """Transform the Snowflake data into a ready-to-use Pytorch datapipe.

        Return a Pytorch datapipe which iterates on rows of data.

        Args:
            batch_size: It specifies the size of each data batch which will be
                yield in the result datapipe
            shuffle: It specifies whether the data will be shuffled. If True, files will be shuffled, and
                rows in each file will also be shuffled.
            drop_last_batch: Whehter the last batch of data should be dropped. If set to be true,
                then the last batch will get dropped if its size is smaller than the given batch_size.

        Returns:
            A Pytorch iterable datapipe that yield data.

        Examples:
        >>> conn = snowflake.connector.connect(**connection_parameters)
        >>> fileset = FileSet.make(
        >>>     sf_connection=conn, name="helloworld", target_stage_loc="@mydb.myschema.mystage"
        >>>     query="SELECT * FROM Mytable"
        >>> )
        >>> dp = fileset.to_torch_datapipe(batch_size=1)
        >>> for data in dp:
        >>>     print(data)
        ----
        {'_COL_1':[10]}
        """
        IterableWrapper, _ = import_utils.import_or_get_dummy("torchdata.datapipes.iter.IterableWrapper")
        torch_datapipe_module, _ = import_utils.import_or_get_dummy("snowflake.ml.fileset.torch_datapipe")

        self._fs.optimize_read(self.files())

        input_dp = IterableWrapper(self.files())
        return torch_datapipe_module.ReadAndParseParquet(input_dp, self._fs, batch_size, shuffle, drop_last_batch)

    @telemetry.send_api_usage_telemetry(
        project=_PROJECT,
        func_params_to_log=["batch_size", "shuffle", "drop_last_batch"],
    )
    @_raise_if_deleted
    def to_tf_dataset(self, *, batch_size: int, shuffle: bool = False, drop_last_batch: bool = True) -> Any:
        """Transform the Snowflake data into a ready-to-use TensorFlow tf.data.Dataset.

        Args:
            batch_size: It specifies the size of each data batch which will be
                yield in the result datapipe
            shuffle: It specifies whether the data will be shuffled. If True, files will be shuffled, and
                rows in each file will also be shuffled.
            drop_last_batch: Whehter the last batch of data should be dropped. If set to be true,
                then the last batch will get dropped if its size is smaller than the given batch_size.

        Returns:
            A tf.data.Dataset that yields batched tf.Tensors.

        Examples:
        >>> conn = snowflake.connector.connect(**connection_parameters)
        >>> fileset = FileSet.make(
        >>>     sf_connection=conn, name="helloworld", target_stage_loc="@mydb.myschema.mystage"
        >>>     query="SELECT * FROM Mytable"
        >>> )
        >>> dp = fileset.to_tf_dataset(batch_size=1)
        >>> for data in dp:
        >>>     print(data)
        ----
        {'_COL_1': <tf.Tensor: shape=(1,), dtype=int64, numpy=[10]>}
        """
        tf_dataset_module, _ = import_utils.import_or_get_dummy("snowflake.ml.fileset.tf_dataset")

        self._fs.optimize_read(self.files())

        return tf_dataset_module.read_and_parse_parquet(self.files(), self._fs, batch_size, shuffle, drop_last_batch)

    @_raise_if_deleted
    def delete(self) -> None:
        """Delete the FileSet directory and all the stage files in it.

        If not called, the FileSet and all its stage files will stay in Snowflake stage.

        Raises:
            FileSetCannotDeleteError: An error occured when the FileSet cannot get deleted.
        """
        delete_sql = f"remove {self._fileset_absolute_path()}"
        try:
            self._snowpark_session.sql(delete_sql).collect(
                statement_params=telemetry.get_function_usage_statement_params(
                    project=_PROJECT,
                    function_name=telemetry.get_statement_params_full_func_name(
                        inspect.currentframe(), self.__class__.__name__
                    ),
                ),
            )
            self._files = []
            self._is_deleted = True
        except exceptions.SnowparkClientException as e:
            raise fileset_errors.FileSetCannotDeleteError(e)
        return


def _validate_files_from_same_query(files: List[str], fileset_absolute_path: str) -> None:
    """Validate that all the files in the FileSet are generated from the same query."""
    valid = True
    if files:
        common_prefix = f"sfc://{fileset_absolute_path}data_"
        common_prefix_len = len(common_prefix)
        query_id = None
        for file in files:
            if len(file) < common_prefix_len:
                valid = False
                break
            truncatred_filename = file[common_prefix_len:]
            if query_id:
                if not truncatred_filename.startswith(query_id):
                    valid = False
                    break
            else:
                idx = truncatred_filename.find("_")
                query_id = truncatred_filename[:idx]
    if not valid:
        raise fileset_errors.MoreThanOneQuerySourceError("This FileSet contains files generated by the other queries.")


def _validate_target_stage_loc(snowpark_session: snowpark.Session, target_stage_loc: str) -> bool:
    """Validate the input stage location is in the right format and the target stage is an internal SSE stage.

    A valid format for the input stage location should be '@<database>.<schema>.<stage>/<optional_directories>/',
        where '<database>', '<schema>' and '<stage>' are all snowflake identifiers.

    Args:
        snowpark_session: A snowpark session.
        target_stage_loc: Path to the target location. Should be in the form of
            '@<database>.<schema>.<stage>/<optional_directories>/'

    Returns:
        A Boolean value about whether the input target stage location is a valid path in an internal SSE stage.

    Raises:
        FileSetLocationError: An error occured when the input stage path is invalid.
    """
    if not target_stage_loc.startswith("@"):
        raise fileset_errors.FileSetLocationError('FileSet location should start with "@".')
    try:
        db, schema, stage, _ = identifier.parse_schema_level_object_identifier(target_stage_loc[1:])
        df_stages = snowpark_session.sql(f"Show stages like '{stage}' in SCHEMA {db}.{schema}")
        df_stages = df_stages.filter(
            functions.col('"type"').like(snowpark_types.ColumnOrLiteralStr(f"%{_FILESET_STAGE_TYPE}%"))
        )
        valid_stage = df_stages.collect()
        if not valid_stage:
            raise fileset_errors.FileSetLocationError(
                "A FileSet requires its location to be in an existing server-side-encrypted internal stage."
                "See https://docs.snowflake.com/en/sql-reference/sql/create-stage#internal-stage-parameters-internalstageparams "  # noqa: E501
                "on how to create such a stage."
            )
    except ValueError as e:
        raise fileset_errors.FileSetLocationError(e)
    return True


def _fileset_absoluate_path(target_stage_loc: str, fileset_name: str) -> str:
    """Get the Snowflake absoluate path to a FileSet.

    Args:
        target_stage_loc: A string of the location where the FileSet lives in. It should be in the form of
            '@<database>.<schema>.<stage>/<optional_directories>/', where
            '<database>', '<schema>' and '<stage>' are all snowflake identifiers.
            A trailing '/' will be added to the location if there is no one present in the given string.
        fileset_name: The name of the FileSet.

    Returns:
        The absolute path to a FileSet in Snowflake. It is supposed to be in the form of
            '@<database>.<schema>.<stage>/<optional_directories>/<fileset_name>/'.
    """
    target_fileset_loc = target_stage_loc
    if not target_fileset_loc.endswith("/"):
        target_fileset_loc += "/"
    target_fileset_loc += fileset_name + "/"
    return target_fileset_loc


def _cast_snowpark_dataframe(df: snowpark.DataFrame) -> snowpark.DataFrame:
    """Cast columns in the dataframe to types that are compatible with tensor.

    Dataframe columns will be casted by the following rules:
    - Data casting:
        - DecimalType(NUMBER):
            - If its scale is zero, cast to BIGINT
            - If its scale is non-zero, cast to FLOAT
        - DoubleType(DOUBLE): Cast to FLOAT.
        - ByteType(TINYINT): Cast to SMALLINT.
        - ShortType(SMALLINT):Cast to SMALLINT.
        - IntegerType(INT): Cast to INT.
        - LongType(BIGINT): Cast to BIGINT.
    - No action:
        - FloatType(FLOAT): No action.
        - StringType(String): No action.
        - BinaryType(BINARY): No action.
        - BooleanType(BOOLEAN): No action.
    - Not supported:
        - ArrayType(ARRAY): Not supported. A warning will be logged.
        - MapType(OBJECT): Not supported. A warning will be logged.
        - TimestampType(TIMESTAMP): Not supported. A warning will be logged.
        - TimeType(TIME): Not supported. A warning will be logged.
        - DateType(DATE): Not supported. A warning will be logged.
        - VariantType(VARIANT): Not supported. A warning will be logged.

    Args:
        df: A snowpark dataframe.

    Returns:
        A snowpark dataframe whose data type has been casted.
    """

    fields = df.schema.fields
    selected_cols = []
    for field in fields:
        src = field.column_identifier.quoted_name
        if isinstance(field.datatype, types.DecimalType):
            if field.datatype.scale:
                dest: types.DataType = types.FloatType()
            else:
                dest = types.LongType()
            selected_cols.append(functions.cast(snowpark_types.ColumnOrName(functions.col(src)), dest).alias(src))
        elif isinstance(field.datatype, types.DoubleType):
            dest = types.FloatType()
            selected_cols.append(functions.cast(snowpark_types.ColumnOrName(functions.col(src)), dest).alias(src))
        elif isinstance(field.datatype, types.ByteType):
            # Snowpark maps ByteType to BYTEINT, which will not do the casting job when unloading to parquet files.
            # We will use SMALLINT instead until this issue got fixed.
            # Investigate JIRA filed: SNOW-725041
            dest = types.ShortType()
            selected_cols.append(functions.cast(snowpark_types.ColumnOrName(functions.col(src)), dest).alias(src))
        elif field.datatype in (types.ShortType(), types.IntegerType(), types.LongType()):
            dest = field.datatype
            selected_cols.append(functions.cast(snowpark_types.ColumnOrName(functions.col(src)), dest).alias(src))
        else:
            if field.datatype in (types.DateType(), types.TimestampType(), types.TimeType()):
                logging.warning(
                    "A Column with DATE or TIMESTAMP data type detected. "
                    "It might not be able to get converted to tensors. "
                    "Please consider handle it in feature engineering."
                )
            elif (
                isinstance(field.datatype, types.ArrayType)
                or isinstance(field.datatype, types.MapType)
                or isinstance(field.datatype, types.VariantType)
            ):
                logging.warning(
                    "A Column with semi-structured data type (variant, array or object) was detected. "
                    "It might not be able to get converted to tensors. "
                    "Please consider handling it in feature engineering."
                )
            selected_cols.append(functions.col(src))
    df = df.select(selected_cols)
    return df